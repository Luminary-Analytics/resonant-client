"""Write THIRD_PARTY_NOTICES.txt for the bundle, and add bundled files to an SBOM.

Run with the build environment's Python, after the release dependencies are
installed and before PyInstaller runs, so the notices describe exactly the
packages the bundle is built from:

    python packaging/third_party_notices.py --out build/licenses/THIRD_PARTY_NOTICES.txt

Python packages come from the environment's metadata, with the license texts
they ship; a package that ships none (its wheel and source distribution carry
no license file) gets a committed copy from its repository, for its version
(``python_packages`` in the components file). Everything else Lumi bundles
(ripgrep and the crates it links, WinSparkle and the libraries in it, the
WebView2 SDK's DLLs, the vendored web assets and fonts, the Python runtime and
PyInstaller's bootloader) is listed in packaging/third-party-components.json,
each with its license text: a copy committed for its version, or, for the
Python runtime and PyInstaller's bootloader, the text this build's own Python
and PyInstaller ship, with their exact versions. Writing the notices fails
when anything shipped, a Python package or not, would ship without its
license text (the EULA's section 5.1 promises every one), when a package's
committed text is for another version, or when the build's Python or
PyInstaller isn't the pinned one.

With --sbom, the same non-Python components are appended to an existing
CycloneDX JSON document (made by `cyclonedx-py environment`), so the SBOM names
every third-party part of the installer, not only the Python packages. It runs
in the Python that makes the SBOM, not the build environment, so PyInstaller's
bootloader takes the version packaging/requirements-release.txt locks, which
the build installed. Add --validate to check the result against the CycloneDX
schema; that needs cyclonedx-bom (packaging/tools-requirements.txt) in the
Python running it.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "packaging" / "third-party-components.json"
# The release lock: what the build environment installs (scripts/lock_release.py writes it).
LOCK = ROOT / "packaging" / "requirements-release.txt"

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


def python_packages(distributions=None, *, skip: set[str] = frozenset(),
                    committed: dict[str, dict] | None = None) -> list[dict]:
    """Installed packages with their licenses; ``distributions`` defaults to all.

    ``skip`` names packages that are installed but not shipped. ``committed`` (by default
    ``python_packages`` in the components file) gives the license, and a committed copy of its text,
    for packages that ship none; each such entry records the version its text is for (``pinned``),
    which ``package_problems`` compares.
    """
    committed = committed_package_licenses() if committed is None else committed
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
        entry = committed.get(key)
        if entry:
            seen[key].update(license=entry["license"], url=entry.get("url") or seen[key]["url"],
                             note=entry.get("note", ""), pinned=str(entry["version"]),
                             texts=component_texts(entry) + seen[key]["texts"])
    return sorted(seen.values(), key=lambda item: normalize(item["name"]))


def committed_package_licenses(path: Path = COMPONENTS) -> dict[str, dict]:
    """``python_packages`` in the components file: {normalized name: entry}."""
    data = json.loads(path.read_text(encoding="utf-8")).get("python_packages") or {}
    return {normalize(name): entry for name, entry in data.items() if not name.startswith("_")}


def package_problems(packages: list[dict]) -> list[str]:
    """Shipped Python packages without a license text, or whose committed text is for another version."""
    problems = []
    for item in packages:
        if item.get("pinned") and item["version"] != item["pinned"]:
            problems.append(f"{item['name']} {item['version']} isn't {item['pinned']}, the version whose license "
                            "text python_packages holds")
        elif not item["texts"]:
            problems.append(f"{item['name']} {item['version']} ships no license text")
    return problems


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


def python_license() -> list[tuple[str, str]]:
    """The license Python ships, from the Python running this script: the build's, which PyInstaller embeds.

    LICENSE.txt sits in the installation's root on Windows and beside the standard library elsewhere; it
    holds the PSF license and the licenses of the software Python includes.
    """
    candidates = [Path(sys.base_prefix) / "LICENSE.txt", Path(sysconfig.get_paths()["stdlib"]) / "LICENSE.txt"]
    for path in candidates:
        if path.is_file():
            return [(path.name, path.read_text(encoding="utf-8", errors="replace").strip())]
    return []


def distribution_license(name: str) -> tuple[str, list[tuple[str, str]]]:
    """(version, license texts) of an installed package, such as PyInstaller for its bootloader; ('', [])
    when it isn't installed."""
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        return "", []
    return dist.version, license_texts(dist)


def component_texts(component: dict) -> list[tuple[str, str]]:
    """A component's license texts: its committed ``license_files``, or with ``license_from`` the build's own
    copy (``python``: the running Python's; ``distribution:<name>``: that installed package's)."""
    texts = []
    for relative in component.get("license_files", []):
        file = ROOT / relative
        if file.is_file():
            texts.append((file.name, file.read_text(encoding="utf-8", errors="replace").strip()))
    source = str(component.get("license_from") or "")
    if source == "python":
        texts += python_license()
    elif source.startswith("distribution:"):
        texts += distribution_license(source.split(":", 1)[1])[1]
    return texts


def locked_version(name: str, lock: Path = LOCK) -> str:
    """The version the release lock pins for package ``name``, or ''."""
    wanted = normalize(name)
    for line in lock.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)", line)
        if match and normalize(match.group(1)) == wanted:
            return match.group(2)
    return ""


def resolved(component: dict, *, locked: bool = False) -> dict:
    """``component`` with the exact version the build ships, for one whose text comes from the build
    (``license_from``): the running Python's (``3.13.7``) or the installed package's. The file's version is
    the pin: a build on another Python minor, or another major of the package, fails (``version_problems``).

    ``locked`` takes a package's version from the release lock instead, for the SBOM, which is made outside
    the build environment (the build installed exactly the locked version).
    """
    source = str(component.get("license_from") or "")
    if source == "python":
        return {**component, "version": platform.python_version()}
    if source.startswith("distribution:"):
        name = source.split(":", 1)[1]
        version = locked_version(name) if locked else distribution_license(name)[0]
        return {**component, "version": version} if version else dict(component)
    return dict(component)


def version_problems(components: list[dict]) -> list[str]:
    """Components whose version in the build isn't the one the components file pins, or, for a package's
    (``distribution:<name>``), the one the release lock pins, which the SBOM names (``resolved``)."""
    problems = []
    for component in components:
        if not component.get("license_from"):
            continue
        actual = resolved(component)["version"]
        pinned = str(component["version"])
        if actual != pinned and not actual.startswith(pinned + "."):
            problems.append(f"{component['name']} {actual} isn't {pinned} (packaging/third-party-components.json)")
            continue
        locked = resolved(component, locked=True)["version"]
        if str(component["license_from"]).startswith("distribution:") and actual != locked:
            problems.append(f"{component['name']} {actual} isn't {locked}, the version "
                            "packaging/requirements-release.txt locks")
    return problems


def missing_texts(components: list[dict]) -> list[str]:
    """The components that would ship without their license text (EULA 5.1 promises every one)."""
    return [f"{item['name']} {resolved(item)['version']}" for item in components if not component_texts(item)]


def render(packages: list[dict], components: list[dict]) -> str:
    lines = [
        "Lumi third-party notices",
        "",
        "Lumi includes the software listed below. Each part remains under its own",
        "license; the license texts that each part ships are reproduced here. Lumi",
        # The bundle doesn't ship the repository's LICENSE, so say it here, as
        # Settings > About Lumi does.
        "itself is © Luminary Analytics, LLC, all rights reserved, and licensed",
        "under the Lumi End User License Agreement.",
        "",
        "Contents",
        "",
    ]
    entries = [
        {**item, "kind": "Python package"} for item in packages
    ] + [
        {**resolved(item), "kind": item.get("kind", "Bundled component"), "texts": component_texts(item)}
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
        # main() refuses to write notices with a part that has no license text (package_problems,
        # missing_texts).
        for filename, body in entry["texts"]:
            lines += ["", f"--- {filename} ---", "", body]
    return "\n".join(lines) + "\n"


def add_to_sbom(sbom_path: Path, components: list[dict]) -> int:
    """Append the non-Python components to a CycloneDX JSON document."""
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))
    listed = sbom.setdefault("components", [])
    refs = {item.get("bom-ref") for item in listed}
    added = 0
    for component in (resolved(item, locked=True) for item in components):
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
        # Everything shipped comes with its license text (the EULA says so), from the version that ships:
        # Python packages and the other components alike.
        problems = package_problems(packages) + version_problems(components) + [
            f"{name} has no license text" for name in missing_texts(components)]
        if problems:
            print("The third-party notices would be incomplete: " + "; ".join(problems) + ". Add the text "
                  "(python_packages, or license_files or license_from, in "
                  "packaging/third-party-components.json) or build with the pinned versions.", file=sys.stderr)
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
