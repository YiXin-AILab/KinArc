"""模块导入边界回归测试。"""

import ast
import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parents[2]
_AGENTSYSTEM_ROOT = _PROJECT_ROOT / "agentsystem"
_SERVER_ROOT = _PROJECT_ROOT / "server"


def test_internal_modules_do_not_declare_behavior_exception_classes() -> None:
    """内部校验使用内置异常，跨边界行为异常只能来自 common。"""
    internal_roots = (
        "cancellation",
        "asset",
        "config",
        "context",
        "inbox",
        "message",
        "middleware",
        "outbound",
        "redis",
        "skill",
        "state",
        "service",
        "schedule",
        "tool",
    )
    offenders = {
        f"{path.relative_to(_PROJECT_ROOT)}:{node.name}"
        for root_name in internal_roots
        for path in (_AGENTSYSTEM_ROOT / root_name).rglob("*.py")
        for node in ast.walk(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        if isinstance(node, ast.ClassDef)
        and any(
            (base_name := _base_name(base)) is not None
            and (base_name.endswith("Error") or base_name == "Exception")
            for base in node.bases
        )
    }

    assert offenders == set()

    server_offenders = {
        f"{path.relative_to(_PROJECT_ROOT)}:{node.name}"
        for path in _SERVER_ROOT.rglob("*.py")
        for node in ast.walk(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        if isinstance(node, ast.ClassDef)
        and any(
            (base_name := _base_name(base)) is not None
            and (base_name.endswith("Error") or base_name == "Exception")
            for base in node.bases
        )
    }
    assert server_offenders == set()


def test_production_trace_attributes_are_vendor_neutral() -> None:
    """生产模块不得重新写入 Langfuse 专属 Span 属性。"""
    vendor_literals = {
        node.value
        for path in _production_python_files()
        for node in ast.walk(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("langfuse.")
    }

    assert vendor_literals == set()


def test_import_parser_resolves_relative_imports(tmp_path: Path) -> None:
    """相对导入必须规范化，否则边界门禁可被语法形式绕过。"""
    module = tmp_path / "agentsystem/domain/protocol.py"
    module.parent.mkdir(parents=True)
    module.write_text("from ..redis import client\n", encoding="utf-8")

    assert _imports(module, project_root=tmp_path) == {"agentsystem.redis"}


def _imports(
    path: Path,
    *,
    project_root: Path = _PROJECT_ROOT,
) -> set[str]:
    """返回模块中的绝对 import 目标。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    relative = path.relative_to(project_root).with_suffix("")
    module_parts = relative.parts
    package_parts = module_parts[:-1]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module is not None:
                    imported.add(node.module)
                continue
            keep = len(package_parts) - (node.level - 1)
            base = package_parts[: max(0, keep)]
            if node.module is not None:
                imported.add(".".join((*base, *node.module.split("."))))
            else:
                imported.update(".".join((*base, alias.name)) for alias in node.names)
    return imported


def _production_python_files() -> tuple[Path, ...]:
    """返回受架构门禁约束的生产源码文件。"""
    return tuple((*_AGENTSYSTEM_ROOT.rglob("*.py"), *_SERVER_ROOT.rglob("*.py")))


def _base_name(base: ast.expr) -> str | None:
    """返回直接基类表达式末段名称。"""
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    return None


def _defines_protocol(path: Path) -> bool:
    """判断模块是否直接声明 Protocol。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        isinstance(node, ast.ClassDef)
        and any(
            (isinstance(base, ast.Name) and base.id == "Protocol")
            or (isinstance(base, ast.Attribute) and base.attr == "Protocol")
            for base in node.bases
        )
        for node in tree.body
    )


def _is_http_layer_import(module: str) -> bool:
    """识别 Server 和 HTTP/ASGI 框架，避免源码字符串检查误伤注释。"""
    return module.split(".", 1)[0] in {"server", "fastapi", "starlette"}


def test_agentsystem_never_imports_http_layer() -> None:
    """核心 Runtime 不得反向依赖 HTTP/ASGI 层。"""
    offenders = {
        str(path.relative_to(_PROJECT_ROOT)): sorted(
            name for name in _imports(path) if _is_http_layer_import(name)
        )
        for path in _AGENTSYSTEM_ROOT.rglob("*.py")
        if any(_is_http_layer_import(name) for name in _imports(path))
    }

    assert offenders == {}


def test_agentsystem_never_imports_application_tools() -> None:
    """应用工具只能由应用或评测装配，Runtime 不得反向依赖 application。"""
    offenders = {
        str(path.relative_to(_PROJECT_ROOT)): sorted(
            name for name in _imports(path) if name.split(".", 1)[0] == "application"
        )
        for path in _AGENTSYSTEM_ROOT.rglob("*.py")
        if any(name.split(".", 1)[0] == "application" for name in _imports(path))
    }
    assert offenders == {}


def test_server_business_modules_only_import_service_boundary() -> None:
    """Server 业务模块不得越过 Service 依赖 Runtime 或领域 Adapter。"""
    forbidden = (
        "agentsystem.runtime",
        "agentsystem.outbound",
        "agentsystem.asset",
    )
    offenders = {
        str(path.relative_to(_PROJECT_ROOT)): sorted(
            name for name in _imports(path) if name.startswith(forbidden)
        )
        for path in _SERVER_ROOT.rglob("*.py")
        if any(name.startswith(forbidden) for name in _imports(path))
    }

    assert offenders == {}


def test_runtime_never_imports_service() -> None:
    """单 Agent Runtime 内核不得反向依赖节点级 Service。"""
    service_prefix = "agentsystem.service"
    runtime_root = _AGENTSYSTEM_ROOT / "runtime"
    offenders = {
        str(path.relative_to(_PROJECT_ROOT)): sorted(
            name
            for name in _imports(path)
            if name == service_prefix or name.startswith(f"{service_prefix}.")
        )
        for path in runtime_root.rglob("*.py")
        if any(
            name == service_prefix or name.startswith(f"{service_prefix}.")
            for name in _imports(path)
        )
    }

    assert offenders == {}


def test_protocol_modules_do_not_import_redis() -> None:
    """领域 Protocol 不得携带 Redis SDK 或项目 Redis 基础设施类型。"""
    protocol_modules = [
        path for path in _AGENTSYSTEM_ROOT.rglob("*.py") if _defines_protocol(path)
    ]
    offenders = {
        str(path.relative_to(_PROJECT_ROOT)): sorted(
            name
            for name in _imports(path)
            if name == "redis"
            or name.startswith("redis.")
            or name == "agentsystem.redis"
            or name.startswith("agentsystem.redis.")
        )
        for path in protocol_modules
        if any(
            name == "redis"
            or name.startswith("redis.")
            or name == "agentsystem.redis"
            or name.startswith("agentsystem.redis.")
            for name in _imports(path)
        )
    }

    assert offenders == {}


def test_redis_adapters_do_not_import_sibling_domains() -> None:
    """领域 Redis adapter 只能依赖本领域和无业务语义的 Redis 基础设施。"""
    domains = frozenset({"inbox", "lock", "message", "state", "outbound"})
    offenders: dict[str, list[str]] = {}
    for domain in domains:
        forbidden = tuple(f"agentsystem.{name}." for name in domains - {domain})
        for path in _redis_adapters(_AGENTSYSTEM_ROOT / domain):
            imported = sorted(
                name for name in _imports(path) if name.startswith(forbidden)
            )
            if imported:
                offenders[str(path.relative_to(_PROJECT_ROOT))] = imported

    assert offenders == {}


def _redis_adapters(domain_root: Path) -> list[Path]:
    """发现直接使用共享 Redis client 生命周期的领域 adapter。"""
    return [
        path
        for path in domain_root.rglob("*.py")
        if any(
            name == "agentsystem.redis.client"
            or name.startswith("agentsystem.redis.client.")
            for name in _imports(path)
        )
    ]


def test_litellm_provider_imports_in_a_fresh_interpreter() -> None:
    """LLM adapter 不应依赖 pytest 收集顺序规避循环导入。"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from agentsystem.llm.litellm_provider import LiteLLMProvider",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        # LiteLLM import 默认远程拉取 model cost map，弱网下 read timeout
        # 会耗尽子进程 10s 上限；强制本地副本使该测试不依赖公网。
        env={**os.environ, "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
    )

    assert result.returncode == 0, result.stderr


def test_vertex_ai_dependencies_import_in_a_fresh_interpreter() -> None:
    """标准安装必须包含 LiteLLM Vertex AI 的 Google auth 依赖。"""
    result = subprocess.run(
        [sys.executable, "-c", "import google.auth"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
