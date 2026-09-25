"""Write THIRD_PARTY_NOTICES.txt for the bundle, and add bundled files to an SBOM.

Run with the build environment's Python, after the release dependencies are
installed and before PyInstaller runs, so the notices describe exactly the
packages the bundle is built from:

    python packaging/third_party_notices.py --out build/licenses/THIRD_PARTY_NOTICES.txt

Python packages come from the environment's metadata, with the license texts
they ship. Everything else Lumi bundles (ripgrep, WinSparkle, the vendored web
assets and fonts, the Python runtime and PyInstaller's bootloader) is listed in
packaging/third-party-components.json.

With --sbom, the same non-Python components are appended to an existing
CycloneDX JSON document (made by `cyclonedx-py environment`), so the SBOM names
every third-party part of the installer, not only the Python packages. Add
--validate to check the result against the CycloneDX schema; that needs
cyclonedx-bom (packaging/tools-requirements.txt) in the Python running it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "packaging" / "third-party-components.json"

# Build tooling installed next to the release dependencies but not shipped.
# PyInstaller's bootloader is shipped; it is listed in the components file.
BUILD_ONLY = {"pip", "wheel", "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile", "macholib"}
# Lumi itself: its own license is the repository's LICENSE.
SELF = {"lumi"}
# Licenses that would impose their terms on the distributed bundle. A shipped
# Python package under one of them fails the build unless the components file
# records a review (`license_reviews`) or excludes it (`not_shipped`).
COPYLEFT = re.compile(r"\b(A|L)?GPL|General Public License|\bEUPL|\bSSPL|\bOSL-", re.IGNORECASE)

_LICENSE_FILE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$", re.IGNORECASE)
_RULE = "=" * 78


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def license_of(dist: metadata.Distribution) -> str:
    """The best available license statement: an SPDX expression, text or classifiers."""
    meta = dist.metadata
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression
    classifiers = [
        value.split("::")[-1].strip()
        for value in meta.get_all("Classifier") or []
        if value.startswith("License ::") and value.count("::") >= 2
    ]
    text = (meta.get("License") or "").strip()
    # A short License field is a name; a long one is the whole license text,
    # which appears below with the files anyway.
    if text and len(text) <= 120 and "\n" not in text:
        return text
    if classifiers:
        return " AND ".join(dict.fromkeys(classifiers))
    return "See the license text below" if text else "Not stated in package metadata"


def homepage_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    for value in meta.get_all("Project-URL") or []:
        label, _, url = value.partition(",")
        if label.strip().lower() in {"homepage", "home", "source", "source code", "repository"}:
            return url.strip()
    return (meta.get("Home-page") or "").strip()


def license_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    texts: list[tuple[str, str]] = []
    for file in dist.files or []:
        path = str(file).replace("\\", "/")
        if ".dist-info/" not in path and ".egg-info/" not in path:
            continue
        if not (_LICENSE_FILE.search(path) or "/licenses/" in path):
            continue
        try:
            body = file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if body and body.strip():
            texts.append((path.rsplit("/", 1)[-1], body.strip()))
    if not texts:
        text = (dist.metadata.get("License") or "").strip()
        if len(text) > 120 or "\n" in text:
            texts.append(("License (from package metadata)", text))
    return texts


def python_packages(distributions=None, *, skip: set[str] = frozenset()) -> list[dict]:
    """Installed packages with their licenses; ``distributions`` defaults to all.

    ``skip`` names packages that are installed but not shipped.
    """
    seen: dict[str, dict] = {}
    skipped = BUILD_ONLY | SELF | {normalize(name) for name in skip}
    for dist in metadata.distributions() if distributions is None else distributions:
        name = dist.metadata.get("Name") or ""
        key = normalize(name)
        if not name or key in skipped or key in seen:
            continue
        seen[key] = {
            "name": name,
            "version": dist.version,
            "license": license_of(dist),
            "url": homepage_of(dist),
            "texts": license_texts(dist),
        }
    return sorted(seen.values(), key=lambda item: normalize(item["name"]))


def load_components(path: Path = COMPONENTS, platform: str = sys.platform) -> list[dict]:
    """The bundled non-Python components; ``platforms`` limits one to some builds."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [item for item in data["components"] if platform in item.get("platforms", [platform])]


def _named(path: Path, key: str) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8")).get(key) or {}
    return {name: reason for name, reason in data.items() if not name.startswith("_")}


def not_shipped(path: Path = COMPONENTS) -> dict[str, str]:
    """Installed packages the bundle leaves out (lumi.spec excludes them), with why."""
    return _named(path, "not_shipped")


def copyleft_problems(packages: list[dict], path: Path = COMPONENTS) -> list[str]:
    """Shipped packages whose copyleft license has not been reviewed."""
    reviewed = {normalize(name) for name in _named(path, "license_reviews")}
    return [
        f"{item['name']} {item['version']} is {item['license']}"
        for item in packages
        if COPYLEFT.search(item["license"]) and normalize(item["name"]) not in reviewed
    ]


def component_texts(component: dict) -> list[tuple[str, str]]:
    texts = []
    for relative in component.get("license_files", []):
        file = ROOT / relative
        if file.is_file():
            texts.append((file.name, file.read_text(encoding="utf-8", errors="replace").strip()))
    return texts


def render(packages: list[dict], components: list[dict]) -> str:
    lines = [
        "Lumi third-party notices",
        "",
        "Lumi includes the software listed below. Each part remains under its own",
        "license; the license texts that each part ships are reproduced here. Lumi",
        "itself is licensed as stated in its LICENSE file.",
        "",
        "Contents",
        "",
    ]
    entries = [
        {**item, "kind": "Python package"} for item in packages
    ] + [
        {**item, "kind": item.get("kind", "Bundled component"), "texts": component_texts(item)}
        for item in components
    ]
    for entry in entries:
        lines.append(f"  {entry['name']} {entry['version']} — {entry['license']}")
    for entry in entries:
        lines += ["", _RULE, f"{entry['name']} {entry['version']} ({entry['kind']})", _RULE,
                  f"License: {entry['license']}"]
        if entry.get("url"):
            lines.append(f"Source: {entry['url']}")
        if entry.get("note"):
            lines.append(entry["note"])
        for filename, body in entry["texts"]:
            lines += ["", f"--- {filename} ---", "", body]
        if not entry["texts"]:
            lines += ["", "No license file is distributed with this part; see the source above."]
    return "\n".join(lines) + "\n"


def add_to_sbom(sbom_path: Path, components: list[dict]) -> int:
    """Append the non-Python components to a CycloneDX JSON document."""
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    listed = sbom.setdefault("components", [])
    refs = {item.get("bom-ref") for item in listed}
    added = 0
    for component in components:
        ref = component.get("purl") or f"lumi-bundled:{normalize(component['name'])}@{component['version']}"
        if ref in refs:
            continue
        entry = {
            "type": component.get("cyclonedx_type", "library"),
            "bom-ref": ref,
            "name": component["name"],
            "version": component["version"],
            "licenses": [{"expression": component["license"]}],
        }
        if component.get("purl"):
            entry["purl"] = component["purl"]
        if component.get("url"):
            entry["externalReferences"] = [{"type": "website", "url": component["url"]}]
        if component.get("note"):
            entry["description"] = component["note"]
        listed.append(entry)
        added += 1
    sbom_path.write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")
    return added


def validate_sbom(sbom_path: Path) -> str:
    """Return an error message, or an empty string when the SBOM matches its schema."""
    from cyclonedx.schema import SchemaVersion
    from cyclonedx.validation.json import JsonStrictValidator

    text = sbom_path.read_text(encoding="utf-8")
    spec = str(json.loads(text).get("specVersion") or "1.6")
    error = JsonStrictValidator(SchemaVersion["V" + spec.replace(".", "_")]).validate_str(text)
    return "" if error is None else str(error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, help="Where to write THIRD_PARTY_NOTICES.txt")
    parser.add_argument("--sbom", type=Path, help="A CycloneDX JSON file to add bundled components to")
    parser.add_argument("--validate", action="store_true", help="check the SBOM against the CycloneDX schema")
    parser.add_argument("--components", type=Path, default=COMPONENTS)
    args = parser.parse_args(argv)
    if not args.out and not args.sbom:
        parser.error("give --out, --sbom or both")
    components = load_components(args.components)
    if args.out:
        packages = python_packages(skip=set(not_shipped(args.components)))
        problems = copyleft_problems(packages, args.components)
        if problems:
            print(
                "Copyleft licenses would ship in the bundle: " + "; ".join(problems) + ". "
                "Exclude the package (not_shipped in third-party-components.json) or record "
                "a review under license_reviews.",
                file=sys.stderr,
            )
            return 1
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(render(packages, components), encoding="utf-8")
        print(f"Wrote {args.out} ({len(packages)} Python packages, {len(components)} other components)")
    if args.sbom:
        added = add_to_sbom(args.sbom, components)
        print(f"Added {added} bundled components to {args.sbom}")
        if args.validate:
            error = validate_sbom(args.sbom)
            if error:
                print(f"The SBOM does not match the CycloneDX schema: {error}", file=sys.stderr)
                return 1
            print("The SBOM matches the CycloneDX schema.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
