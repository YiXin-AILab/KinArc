"""应用工具变更必须使缓存实验协议失效。"""

from pathlib import Path

import pytest
from benchmarks.evoagentbench.common import identity


@pytest.mark.parametrize("agent", ["knowledge", "research"])
def test_application_tool_change_invalidates_bound_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str
) -> None:
    monkeypatch.setattr(
        identity,
        "__file__",
        str(tmp_path / "benchmarks/evoagentbench/common/identity.py"),
    )
    tool = tmp_path / f"application/evoagentbench_{agent}_agent/plugins/tool.py"
    tool.parent.mkdir(parents=True)
    tool.write_text("VERSION = 1\n")
    manifest = tmp_path / "protocol.txt"
    original = identity.source_digest()
    identity.bind_manifest(manifest, original)
    tool.write_text("VERSION = 2\n")
    changed = identity.source_digest()
    assert changed != original
    with pytest.raises(ValueError, match="protocol changed"):
        identity.bind_manifest(manifest, changed)
