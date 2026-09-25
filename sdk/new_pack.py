"""Start a Lumi extension pack: ``python sdk/new_pack.py <folder> [--name "Acme models"]``.

Copies the provider template and the lumi_extension SDK into a new folder
and names the pack after it. Then edit provider.py, run its tests
(``python -m pytest`` in the folder) and check it as Lumi will
(``lumi extension check <folder>``). See docs/extensions.md.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

SDK = Path(__file__).resolve().parent
TEMPLATES = {"provider": SDK / "templates" / "provider-python"}
_SKIP = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")


def create(folder: str | Path, *, name: str = "", template: str = "provider") -> Path:
    """Make the pack in ``folder``, which must not exist or be empty."""
    folder = Path(folder)
    if folder.exists() and any(folder.iterdir()):
        raise ValueError(f"{folder} isn't empty. Choose a new folder.")
    pack_id = re.sub(r"[^a-z0-9]+", "-", folder.resolve().name.lower()).strip("-")[:40].strip("-") or "my-provider"
    label = " ".join(str(name).split())[:80] or folder.resolve().name
    shutil.copytree(TEMPLATES[template], folder, dirs_exist_ok=True, ignore=_SKIP)
    shutil.copytree(SDK / "python" / "lumi_extension", folder / "lumi_extension", ignore=_SKIP)
    manifest_path = folder / "lumi-pack.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(id=pack_id, name=label)
    manifest["providers"][0].update(id=pack_id, name=label)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return folder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="new_pack.py", description="Start a Lumi extension pack.")
    parser.add_argument("folder", help="Where to make the pack (a new or empty folder)")
    parser.add_argument("--name", default="", help="The pack's name, as Lumi shows it")
    parser.add_argument("--template", choices=sorted(TEMPLATES), default="provider")
    args = parser.parse_args(argv)
    try:
        folder = create(args.folder, name=args.name, template=args.template)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"Made {folder}. Next: cd {folder}; python -m pytest; lumi extension check .")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
