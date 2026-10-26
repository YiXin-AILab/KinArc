"""安全 YAML 加载和递归环境变量插值。"""

import os
import re
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel
from yaml.nodes import MappingNode, ScalarNode

_ENVIRONMENT_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def interpolate_environment(
    value: object,
    environ: Mapping[str, str],
) -> object:
    """递归替换配置值中的 ``${NAME}`` 环境变量占位符。

    Args:
        value: 配置值；映射的键不插值。
        environ: 本次加载使用的环境变量映射。

    Returns:
        替换后的配置值；容器重新构造，其他值原样返回。

    Raises:
        ValueError: 占位符引用了映射中不存在的变量。
    """
    if isinstance(value, str):
        return _interpolate_string(value, environ)
    if isinstance(value, Mapping):
        return {
            key: interpolate_environment(item, environ) for key, item in value.items()
        }
    if isinstance(value, list):
        return [interpolate_environment(item, environ) for item in value]
    if isinstance(value, tuple):
        return tuple(interpolate_environment(item, environ) for item in value)
    return value


def load_yaml_config[ModelT: BaseModel](
    path: str | Path,
    model_type: type[ModelT],
    *,
    environ: Mapping[str, str] = os.environ,
) -> ModelT:
    """读取 YAML 文件、插值环境变量并校验为指定模型。

    Args:
        path: UTF-8 YAML 文件路径。
        model_type: 负责字段及组合约束校验的 Pydantic 模型类。
        environ: 插值来源，默认使用进程环境；不会隐式加载 dotenv。

    Returns:
        完成校验的配置实例。

    Raises:
        OSError: 文件无法读取。
        yaml.YAMLError: YAML 无法解析。
        ValueError: 根节点不是映射、插件选项一级键重复或环境变量缺失。
        pydantic.ValidationError: 插值后的值不符合模型约束。
    """
    config_path = Path(path)
    with config_path.open(encoding="utf-8") as config_file:
        config_text = config_file.read()
    _reject_duplicate_plugin_option_keys(config_text)
    raw_config = yaml.safe_load(config_text)
    if not isinstance(raw_config, Mapping):
        raise ValueError("YAML 配置根节点必须是 mapping")
    interpolated = interpolate_environment(raw_config, environ)
    return model_type.model_validate(interpolated)


def _reject_duplicate_plugin_option_keys(config_text: str) -> None:
    """在 safe_load 合并重复键前检查插件选项，避免配置被静默覆盖。"""
    root = yaml.compose(config_text)
    if not isinstance(root, MappingNode):
        return
    for key_node, value_node in root.value:
        if not isinstance(key_node, ScalarNode) or key_node.value != "plugin_options":
            continue
        if not isinstance(value_node, MappingNode):
            return
        seen: set[str] = set()
        for option_key_node, _ in value_node.value:
            if not isinstance(option_key_node, ScalarNode):
                continue
            option_key = option_key_node.value
            if option_key in seen:
                raise ValueError(f"plugin_options 包含重复一级 key：{option_key}")
            seen.add(option_key)


def _interpolate_string(value: str, environ: Mapping[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        """读取当前占位符的精确变量名，缺失时转为配置校验错误。"""
        variable_name = match.group(1)
        try:
            return environ[variable_name]
        except KeyError as error:
            raise ValueError(f"缺少环境变量：{variable_name}") from error

    return _ENVIRONMENT_PATTERN.sub(replace, value)
