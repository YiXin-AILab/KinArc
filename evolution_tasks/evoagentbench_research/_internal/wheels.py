"""Materialize pure-Python VCS-installed distributions without network/Git paths."""

import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import re
import sys
import zipfile
from pathlib import Path
from typing import Any


def export_vcs_wheels(destination: Path) -> list[dict[str, Any]]:
    """Preserve installed source bytes and licenses; rebuild only wheel bookkeeping."""
    destination.mkdir(parents=True, exist_ok=True)
    records = []
    for distribution in importlib.metadata.distributions():
        direct = json.loads(distribution.read_text("direct_url.json") or "{}")
        if not direct.get("vcs_info"):
            continue
        name = distribution.metadata["Name"]
        wheel = distribution.read_text("WHEEL") or ""
        if "Root-Is-Purelib: true" not in wheel or "Tag: py3-none-any" not in wheel:
            raise ValueError(f"Explicit native VCS wheel export required: {name}")
        normalized = re.sub(r"[-_.]+", "_", name)
        filename = f"{normalized}-{distribution.version}-py3-none-any.whl"
        archive = destination / filename
        contents = []
        dist_info = None
        with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as output:
            for item in distribution.files or []:
                relative = Path(str(item))
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("VCS wheel contains external installation paths")
                if "__pycache__" in relative.parts or relative.suffix == ".pyc":
                    continue
                if relative.parts[0].endswith(".dist-info"):
                    dist_info = relative.parts[0]
                    if relative.name in {
                        "RECORD",
                        "direct_url.json",
                        "uv_build.json",
                        "INSTALLER",
                        "REQUESTED",
                    }:
                        continue
                data = distribution.locate_file(item).read_bytes()
                output.writestr(relative.as_posix(), data)
                digest = (
                    base64.urlsafe_b64encode(hashlib.sha256(data).digest())
                    .rstrip(b"=")
                    .decode()
                )
                contents.append(
                    (relative.as_posix(), "sha256=" + digest, str(len(data)))
                )
            if dist_info is None:
                raise ValueError("VCS distribution lacks metadata")
            record_path = dist_info + "/RECORD"
            stream = io.StringIO()
            csv.writer(stream, lineterminator="\n").writerows(
                [*contents, (record_path, "", "")]
            )
            output.writestr(record_path, stream.getvalue())
        records.append(
            {
                "name": name,
                "version": distribution.version,
                "wheel": filename,
                "vcs_commit": direct["vcs_info"].get("commit_id"),
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "source_files": len(contents),
            }
        )
    return records


if __name__ == "__main__":
    print(json.dumps(export_vcs_wheels(Path(sys.argv[1]))))
