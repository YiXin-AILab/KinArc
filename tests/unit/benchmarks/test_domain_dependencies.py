"""实际依赖冻结应能检测文件内容、文件集合和镜像变化。"""

import json
from pathlib import Path
from typing import Any

import pytest
from benchmarks.evoagentbench.domains import domain_dependencies as dependencies


def test_freeze_checks_actual_content_and_directory_membership(tmp_path: Path) -> None:
    data = tmp_path / "corpus"
    data.mkdir()
    (data / "document").write_text("original")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(dependencies.freeze([data], [], ["http://localhost/mcp"]))
    )
    assert (
        dependencies.validate(manifest)["external_services"]["http://localhost/mcp"][
            "status"
        ]
        == "unverified"
    )
    (data / "extra").write_text("new")
    with pytest.raises(ValueError, match="membership"):
        dependencies.validate(manifest)
    (data / "extra").unlink()
    (data / "document").write_text("mutation")
    with pytest.raises(ValueError, match="changed"):
        dependencies.validate(manifest)


def test_image_tag_drift_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dependencies, "image_id", lambda name: "sha256:old")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(dependencies.freeze([], ["image:tag"], [])))
    dependencies.validate(manifest)
    monkeypatch.setattr(dependencies, "image_id", lambda name: "sha256:new")
    with pytest.raises(ValueError, match="image changed"):
        dependencies.validate(manifest)


def test_reject_placeholder_manifest(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text('{"schema_version":1,"files":[],"images":{}}')
    with pytest.raises(ValueError):
        dependencies.validate(path)


def test_unchanged_file_is_not_rehashed_on_each_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "large-index"
    data.write_bytes(b"index contents")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(dependencies.freeze([data], [], [])))
    dependencies.validate(manifest)

    original_open = Path.open

    def reject_rehash(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if path == data and mode == "rb":
            raise AssertionError("unchanged dependency was read again")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", reject_rehash)
    dependencies.validate(manifest)
