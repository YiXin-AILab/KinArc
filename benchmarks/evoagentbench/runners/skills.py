"""支持清单进化的原生 Skill 资产，并保持固定的评测边界。"""

import json
from pathlib import Path

from agentsystem.common.models import SessionContext, StrictModel
from agentsystem.config.loader import load_yaml_config
from agentsystem.config.models import AgentConfig
from agentsystem.message.models import ToolCallBlock
from agentsystem.skill.index import SkillIndex
from agentsystem.tool.authorization import ToolAuthorizationDecision
from agentsystem.tool.models import ToolDefinition


class SkillManifest(StrictModel):
    """仅允许 Skill 选择发生进化，模型和环境配置保持冻结。"""

    skill_roots: tuple[str, ...] = ()
    enabled_skills: tuple[str, ...] = ()


def configure_skills(target: Path, config: AgentConfig) -> AgentConfig:
    """将候选本地的 Skill 选择覆盖到不可变的 Agent 配置中。"""
    manifest = target / "skills.yaml"
    if manifest.is_symlink():
        raise ValueError("Skill manifest must not be a symlink")
    if manifest.exists():
        selection = load_yaml_config(manifest, SkillManifest, environ={})
        config = AgentConfig.model_validate(
            {**config.model_dump(), **selection.model_dump()}
        )
    # 原生 skill_view 由 Runtime 注册，不属于外部任务工具。
    if config.enabled_skills and "skill_view" not in (config.enabled_tools or ()):
        config = config.model_copy(
            update={"enabled_tools": (*(config.enabled_tools or ()), "skill_view")}
        )
    return config


def skill_roots(target: Path, config: AgentConfig) -> tuple[Path, ...]:
    """仅在冻结候选的 skills/ 资产目录内解析搜索根目录。"""
    roots = []
    for relative in config.skill_roots:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or path.parts[:1] != ("skills",):
            raise ValueError("Skill roots must be relative paths under skills/")
        base = target.resolve()
        root = base / path
        ancestors = (base / parent for parent in (path, *path.parents))
        if any(p.is_symlink() for p in ancestors):
            raise ValueError("Skill roots must not contain symlinks")
        resolved = root.resolve(strict=True)
        resolved.relative_to(target.resolve())
        if not resolved.is_dir():
            raise ValueError("Skill root must be a directory")
        if any(p.is_symlink() for p in resolved.rglob("*")):
            raise ValueError("Skill assets must not contain symlinks")
        roots.append(resolved)
    return tuple(roots)


class CandidateSkillAuthorization:
    """限制原生 skill_view 只能访问已启用的冻结资产，隔离评分数据。"""

    def __init__(self, roots: tuple[Path, ...], enabled: tuple[str, ...]) -> None:
        """为候选已启用的 Skill 资源建立只读索引。

        Args:
            roots: 已校验的候选本地 Skill 搜索目录。
            enabled: 当前试验可使用的 Skill 名称。

        Raises:
            ValueError: Skill 资产或启用配置未通过索引校验。
            KeyError: 已启用的 Skill 在全部搜索根目录中均不存在。
        """
        self._index = SkillIndex.load(roots=roots, enabled_skills=enabled)

    async def authorize(
        self,
        ctx: SessionContext,
        call: ToolCallBlock,
        definition: ToolDefinition,
    ) -> ToolAuthorizationDecision:
        """保留任务工具行为，并校验 Skill 名称及资源路径归属。"""
        if call.tool_name != "skill_view":
            return ToolAuthorizationDecision.allow()
        arguments = call.arguments
        if arguments is None:
            arguments = json.loads(call.raw_arguments)
        name = arguments.get("name")
        logical = arguments.get("file_path") or "SKILL.md"
        record = self._index.get(name) if isinstance(name, str) else None
        if record is None or not isinstance(logical, str):
            return ToolAuthorizationDecision.deny("Skill is not enabled")
        path = Path(logical)
        if path.is_absolute() or ".." in path.parts:
            return ToolAuthorizationDecision.deny("Use a relative Skill resource path")
        resolved = (record.root / path).resolve()
        if (
            not resolved.is_relative_to(record.root)
            or path.as_posix() not in record.resources
        ):
            return ToolAuthorizationDecision.deny(
                "Resource is outside the frozen Skill"
            )
        return ToolAuthorizationDecision.allow()
