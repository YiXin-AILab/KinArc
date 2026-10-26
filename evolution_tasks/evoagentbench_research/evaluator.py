"""官方领域评分器工厂。"""

import importlib.util
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from agentsystem.common.models import JsonValue
from evolution.evaluator import Evaluator


def create_evaluator(task_dir: Path, settings: Mapping[str, JsonValue]) -> Evaluator:
    """为已有官方评分证据创建检索领域评分器。

    Args:
        task_dir: 用于解析相对依赖路径的任务目录。
        settings: Evolution 任务加载器提供的领域配置。

    Returns:
        绑定到已解析检索评分协议的评分器。
    """
    if not settings.get("portable"):
        from benchmarks.evoagentbench.domains.domain_adapter import DomainEvaluator
        from benchmarks.evoagentbench.domains.domain_settings import settings_for

        return DomainEvaluator(settings_for(task_dir, settings))
    path = Path(__file__).parent / "_internal/loading.py"
    spec = importlib.util.spec_from_file_location("_eab_loading", path)
    if spec is None or spec.loader is None:
        raise ImportError("Task-local loading module unavailable")
    loading = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loading)
    return cast(
        Evaluator, loading.adapter(Path(__file__)).Evaluator(task_dir, settings)
    )
