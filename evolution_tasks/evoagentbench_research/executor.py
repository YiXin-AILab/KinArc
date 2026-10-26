"""官方领域执行器工厂。"""

import importlib.util
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from agentsystem.common.models import JsonValue
from evolution.executor import Executor


def create_executor(task_dir: Path, settings: Mapping[str, JsonValue]) -> Executor:
    """根据相对任务目录且已校验的依赖创建检索领域执行器。

    Args:
        task_dir: 用于解析相对依赖路径的任务目录。
        settings: Evolution 任务加载器提供的领域配置。

    Returns:
        绑定到已解析检索环境及试次配置的执行器。
    """
    if not settings.get("portable"):
        from benchmarks.evoagentbench.domains.domain_adapter import DomainExecutor
        from benchmarks.evoagentbench.domains.domain_settings import settings_for

        return DomainExecutor(settings_for(task_dir, settings))
    path = Path(__file__).parent / "_internal/loading.py"
    spec = importlib.util.spec_from_file_location("_eab_loading", path)
    if spec is None or spec.loader is None:
        raise ImportError("Task-local loading module unavailable")
    loading = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loading)
    return cast(Executor, loading.adapter(Path(__file__)).Executor(task_dir, settings))
