"""Create a deterministic bundle manifest and enforce packaging policy."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import sys
from pathlib import Path


def inspect_bundle(bundle_root: Path, policy: dict) -> tuple[dict, list[str]]:
    root = bundle_root.resolve()
    if not root.is_dir():
        return {}, [f"Bundle directory does not exist: {root}"]

    files = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        rel = path.relative_to(root).as_posix()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        files.append({
            "path": rel,
            "size_bytes": path.stat().st_size,
            "sha256": digest.hexdigest(),
        })

    total_bytes = sum(item["size_bytes"] for item in files)
    manifest = {
        "schema_version": 1,
        "bundle_root": str(root),
        "total_bytes": total_bytes,
        "file_count": len(files),
        "largest_files": sorted(
            files, key=lambda item: item["size_bytes"], reverse=True
        )[:20],
        "files": files,
    }
    errors = []

    max_bytes = int(policy.get("max_total_bytes", 0) or 0)
    if max_bytes and total_bytes > max_bytes:
        errors.append(
            f"Bundle size {total_bytes:,} exceeds policy maximum {max_bytes:,} bytes"
        )
    max_files = int(policy.get("max_file_count", 0) or 0)
    if max_files and len(files) > max_files:
        errors.append(
            f"Bundle file count {len(files):,} exceeds policy maximum {max_files:,}"
        )

    paths = [item["path"] for item in files]
    for pattern in policy.get("required_globs", []):
        if not any(fnmatch.fnmatchcase(path, pattern) for path in paths):
            errors.append(f"Required bundle asset is missing: {pattern}")

    forbidden = {
        str(component).lower()
        for component in policy.get("forbidden_path_components", [])
    }
    for path in paths:
        components = {component.lower() for component in Path(path).parts}
        hits = sorted(components & forbidden)
        if hits:
            errors.append(
                f"Forbidden dependency component {', '.join(hits)} found at {path}"
            )

    return manifest, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument(
        "--policy", type=Path,
        default=Path(__file__).with_name("bundle-policy.json"),
    )
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)

    try:
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"bundle policy error: {exc}", file=sys.stderr)
        return 2

    manifest, errors = inspect_bundle(args.bundle, policy)
    if args.manifest and manifest:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    if manifest:
        mib = manifest["total_bytes"] / (1024 * 1024)
        print(f"Bundle: {mib:.1f} MiB, {manifest['file_count']} files")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
