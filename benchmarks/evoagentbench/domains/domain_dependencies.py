"""冻结并校验实际本地依赖，显式标记远端服务状态。"""

import argparse
import hashlib
import json
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any


def file_hash(path: Path) -> str:
    """每个协调器进程仅对未变化的依赖计算一次摘要。"""
    stat = path.stat()
    return _file_hash_for_stat(
        path,
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


@lru_cache(maxsize=4096)
def _file_hash_for_stat(
    path: Path, device: int, inode: int, size: int, mtime_ns: int, ctime_ns: int
) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    current = path.stat()
    if (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    ) != (device, inode, size, mtime_ns, ctime_ns):
        raise ValueError(f"Dependency changed while hashing: {path}")
    return digest.hexdigest()


def image_id(name: str) -> str:
    """将本地 Docker 镜像解析为不可变的内容标识。

    Args:
        name: 待检查的镜像标签或 ID，不触发镜像拉取。

    Returns:
        以 sha256 为前缀的不可变镜像 ID。

    Raises:
        ValueError: Docker 返回的标识缺少预期前缀。
        subprocess.CalledProcessError: Docker 无法检查指定镜像。
        subprocess.TimeoutExpired: 镜像检查超过三十秒。
    """
    value = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", name],
        text=True,
        timeout=30,
    ).strip()
    if not value.startswith("sha256:"):
        raise ValueError("Dependency image has no immutable ID")
    return value


def freeze(paths: list[Path], images: list[str], services: list[str]) -> dict[str, Any]:
    """展开目录，并将 HF 快照中的文件符号链接绑定到实际目标。"""
    files: set[Path] = set()
    directories = []
    for path in paths:
        path = path.absolute()
        if path.is_dir():
            path = path.resolve()
            directories.append(str(path))
            if any(item.is_symlink() and item.is_dir() for item in path.rglob("*")):
                raise ValueError("Pass directory symlink targets explicitly")
            files.update(item for item in path.rglob("*") if item.is_file())
        elif path.is_file():
            files.add(path)
        else:
            raise FileNotFoundError(path)
    if not files and not images:
        raise ValueError("At least one actual file or Docker image is required")
    rows = []
    for path in sorted(files):
        rows.append(
            {
                "path": str(path),
                "resolved_path": str(path.resolve(strict=True)),
                "sha256": file_hash(path),
                "bytes": path.stat().st_size,
            }
        )
    return {
        "schema_version": 1,
        "files": rows,
        "directories": directories,
        "images": {name: image_id(name) for name in images},
        "external_services": {url: {"status": "unverified"} for url in services},
    }


def validate(path: Path) -> dict[str, Any]:
    """根据冻结清单校验摘要、目录文件集合和当前镜像 ID。"""
    manifest = json.loads(path.read_text())
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or not (manifest.get("files") or manifest.get("images"))
    ):
        raise ValueError("Invalid dependency manifest; use domain_dependencies CLI")
    expected = {row["path"] for row in manifest["files"]}
    if len(expected) != len(manifest["files"]):
        raise ValueError("Duplicate dependency file")
    for directory in manifest["directories"]:
        root = Path(directory)
        actual = {str(item) for item in root.rglob("*") if item.is_file()}
        if any(item.is_symlink() and item.is_dir() for item in root.rglob("*")):
            raise ValueError("Frozen dependency directory gained a directory symlink")
        if actual != {item for item in expected if Path(item).is_relative_to(root)}:
            raise ValueError("Frozen dependency directory membership changed")
    for row in manifest["files"]:
        source = Path(row["path"])
        if (
            str(source.resolve(strict=True)) != row["resolved_path"]
            or source.stat().st_size != row["bytes"]
            or file_hash(source) != row["sha256"]
        ):
            raise ValueError("Frozen dependency file changed")
    for name, expected_id in manifest["images"].items():
        if image_id(name) != expected_id:
            raise ValueError("Frozen dependency image changed")
    return manifest


def main() -> None:
    """根据 CLI 参数创建或校验依赖清单。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--path", type=Path, action="append", default=[])
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--external-service", action="append", default=[])
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        validate(args.output)
    else:
        manifest = freeze(args.path, args.image, args.external_service)
        with args.output.open("x") as output:
            json.dump(manifest, output, indent=2)


if __name__ == "__main__":
    main()
