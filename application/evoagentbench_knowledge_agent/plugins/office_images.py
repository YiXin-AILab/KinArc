"""通过现有资产服务和中间件交付隔离工作区中的图片。"""

import base64
import hashlib
import json
import shlex
from collections.abc import AsyncIterable, AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from agentsystem.asset.models import AssetRecord
from agentsystem.common.errors import ToolError
from agentsystem.common.models import JsonValue
from agentsystem.message.models import (
    AssetSource,
    ImageBlock,
    Message,
    MessageVisibility,
    Role,
    TextBlock,
)
from agentsystem.middleware.models import (
    MessagesMiddlewarePayload,
    MiddlewareContext,
    MiddlewareDecision,
    MiddlewareDecisionType,
    MiddlewareFailureMode,
    MiddlewarePoint,
)
from agentsystem.plugin.context import PluginSetupContext
from agentsystem.plugin.models import PluginOrigin, PluginSource
from agentsystem.tool.builder import build_function_tool
from agentsystem.tool.models import ToolDefinition, ToolOutput, ToolSource
from application.evoagentbench_knowledge_agent.plugins.execution import (
    ExecutionEnvironment,
    relative_path,
)


class AssetUploader(Protocol):
    """现有 ApplicationService.upload_asset 调用接口，在初始化后绑定。"""

    async def __call__(
        self,
        body: AsyncIterable[bytes],
        *,
        declared_media_type: str | None,
        filename: str | None,
    ) -> AssetRecord:
        """通过现有资产服务上传有大小限制的图片字节。

        Args:
            body: 包含图片字节的异步数据流。
            declared_media_type: 调用方提供的可选媒体类型。
            filename: 写入资产元数据的可选原始文件名。

        Returns:
            包含检测所得媒体类型的已存储资产记录。
        """
        ...


_SNAPSHOT = """
import hashlib,json,pathlib,sys
root=pathlib.Path.cwd().resolve()
source=(root/sys.argv[1]).resolve()
if not source.is_relative_to(root): raise ValueError('Path escapes workspace')
with source.open('rb') as f: data=f.read(4194305)
if not data or len(data)>4194304: raise ValueError('Image must be 1..4194304 bytes')
pathlib.Path(sys.argv[2]).write_bytes(data)
print(json.dumps({'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}))
"""
_CHUNK = """
import base64,sys
with open(sys.argv[1],'rb') as f:
 f.seek(int(sys.argv[2])); print(base64.b64encode(f.read(45000)).decode())
"""


class OfficeImages:
    """每个隔离试验使用独立图片队列，不访问宿主路径或评分器。"""

    def __init__(
        self,
        environment: ExecutionEnvironment,
        workspace: str,
        *,
        evidence_dir: Path | None = None,
    ) -> None:
        """绑定任务工作区，并初始化图片交付队列。

        Args:
            environment: 调用方持有的容器命令接口。
            workspace: 当前执行环境内的绝对工作区路径。
            evidence_dir: 持久化图片证据的可选宿主目录。
        """
        self.environment, self.workspace = environment, workspace
        self.upload: AssetUploader | None = None
        self.pending: list[tuple[str, ImageBlock]] = []
        self.evidence_dir = evidence_dir
        self.evidence: list[JsonValue] = []

    async def _command(self, command: str) -> str:
        result = await self.environment.run(command, self.workspace, 30)
        if result.exit_code or result.truncated or result.timed_out:
            raise ToolError(code="image_read_failed", message="Image transfer failed")
        return result.stdout

    async def read_image(self, path: str) -> ToolOutput:
        """查看不超过 4 MiB 的图片；PDF 和办公文档需先渲染为图片。

        Args:
            path: 相对于任务工作区的图片文件路径。

        Returns:
            工具确认信息，图片观察会在下一轮交付。

        Raises:
            ToolError: 路径、图片传输、资产服务或媒体类型无效。
        """
        path = relative_path(path)
        if self.upload is None:
            raise ToolError(
                code="image_service_unavailable", message="Asset service unavailable"
            )
        snapshot = "/tmp/eab-image-" + uuid4().hex
        try:
            manifest = json.loads(
                await self._command(
                    f"python3 -c {shlex.quote(_SNAPSHOT)} "
                    f"{shlex.quote(path)} {snapshot}"
                )
            )
            size = manifest["size"]
            if not isinstance(size, int) or not 0 < size <= 4194304:
                raise ToolError(
                    code="invalid_image_size", message="Image exceeds 4 MiB"
                )
            data = bytearray()
            for offset in range(0, size, 45000):
                encoded = await self._command(
                    f"python3 -c {shlex.quote(_CHUNK)} {snapshot} {offset}"
                )
                data.extend(base64.b64decode(encoded.strip(), validate=True))
            if (
                len(data) != size
                or hashlib.sha256(data).hexdigest() != manifest["sha256"]
            ):
                raise ToolError(
                    code="image_integrity_failed",
                    message="Image changed during transfer",
                )

            async def body() -> AsyncIterator[bytes]:
                yield bytes(data)

            asset = await self.upload(
                body(), declared_media_type=None, filename=Path(path).name
            )
            if not asset.media_type.startswith("image/"):
                raise ToolError(
                    code="not_an_image",
                    message="Render this document to an image first",
                )
            if self.evidence_dir:
                self.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                destination = self.evidence_dir / manifest["sha256"]
                destination.write_bytes(data)
                destination.chmod(0o600)
                self.evidence.append(
                    {
                        "asset_id": asset.asset_id,
                        "sha256": manifest["sha256"],
                        "path": str(destination),
                        "media_type": asset.media_type,
                        "original_path": path,
                    }
                )
            self.pending.append(
                (
                    path,
                    ImageBlock(
                        source=AssetSource(
                            asset_id=asset.asset_id,
                            media_type=asset.media_type,
                            filename=asset.filename,
                        )
                    ),
                )
            )
        finally:
            await self._command(f"rm -f {snapshot}")
        return ToolOutput(
            content=f"Image {path} will be displayed before the next model turn."
        )

    async def before_turn(self, context: MiddlewareContext) -> MiddlewareDecision:
        """通过 V1 挂点将图片观察持久化为仅模型可见的 USER 消息。

        此挂点构造消息并清空队列；由 Runtime 在构建下一次模型请求前
        完成消息持久化。

        Args:
            context: BEFORE_TURN 挂点中的当前会话与执行身份。

        Returns:
            队列有图片时返回 MESSAGES 决策，队列为空时返回 CONTINUE。
        """
        if not self.pending:
            return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)
        messages = tuple(
            Message(
                message_id=uuid4().hex,
                session_id=context.ctx.session_id,
                run_id=context.run_id,
                role=Role.USER,
                origin="office_image_tool",
                visibility=MessageVisibility.MODEL,
                created_at=datetime.now(UTC),
                content=(
                    TextBlock(text=f"Image returned by read_image: {path}"),
                    image,
                ),
            )
            for path, image in self.pending
        )
        self.pending.clear()
        return MiddlewareDecision(
            type=MiddlewareDecisionType.MESSAGES,
            payload=MessagesMiddlewarePayload(messages=messages),
        )

    def tool(self) -> ToolDefinition:
        """为当前试验构建串行执行的 read_image 工具。

        Returns:
            绑定到当前实例资产交付队列的工具定义。
        """
        return build_function_tool(
            self.read_image, source=ToolSource.CUSTOMIZED, is_concurrency_safe=False
        )

    def plugin(self) -> PluginSource:
        """注册图片交付逻辑，以及阻止未交付观察进入下一步的保护逻辑。

        Returns:
            提供 BEFORE_TURN 和 BEFORE_LLM 挂点的应用插件。
        """

        async def require_delivery(context: MiddlewareContext) -> MiddlewareDecision:
            """图片仍在队列中时阻止发起模型请求。"""
            del context
            if self.pending:
                raise RuntimeError("Image observations were not persisted before LLM")
            return MiddlewareDecision(type=MiddlewareDecisionType.CONTINUE)

        def register(context: PluginSetupContext) -> None:
            """向 Runtime 注册当前试验的图片中间件。"""
            context.register_middleware(
                self.before_turn,
                name="Office image observations",
                handler_id="images",
                point=MiddlewarePoint.BEFORE_TURN,
                failure_mode=MiddlewareFailureMode.FAIL_OPEN,
            )
            context.register_middleware(
                require_delivery,
                name="Require office image delivery",
                handler_id="require_delivery",
                point=MiddlewarePoint.BEFORE_LLM,
                failure_mode=MiddlewareFailureMode.FAIL_CLOSED,
            )

        return PluginSource(
            plugin_id="office_images",
            origin=PluginOrigin.APPLICATION,
            path=Path(__file__),
            register=register,
        )
