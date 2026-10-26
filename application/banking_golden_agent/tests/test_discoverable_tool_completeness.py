"""Discoverable 工具集合与上游 Banking 元数据的一致性测试。"""

import json
from pathlib import Path

from application.banking_golden_agent.services.discoverable_agent_tools import (
    AGENT_DISCOVERABLE_REGISTRY,
)


def test_agent_registry_exactly_matches_banking_metadata() -> None:
    metadata_path = (
        Path(__file__).resolve().parent.parent / "data/discoverable_tools.json"
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert set(AGENT_DISCOVERABLE_REGISTRY) == set(metadata)


def test_agent_signatures_match_metadata_parameters() -> None:
    metadata_path = (
        Path(__file__).resolve().parent.parent / "data/discoverable_tools.json"
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    for tool_name, tool_metadata in metadata.items():
        definition = AGENT_DISCOVERABLE_REGISTRY[tool_name]
        assert set(definition.signature.parameters) == set(
            tool_metadata["parameters"]
        ), tool_name
