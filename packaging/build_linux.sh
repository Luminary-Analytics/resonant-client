#!/usr/bin/env bash
# Build Lumi for Linux: the one-folder bundle, then a .deb, an .rpm, an AppImage
# and a tarball in dist/installer (docs/deploy-linux.md).
#
#   packaging/build_linux.sh [--sbom dist/lumi-linux-sbom.cdx.json]
#
# Like build_macos.sh: a fresh virtual environment with the hash-pinned
# packaging/requirements-release.txt, the pinned web assets, third-party
# notices, PyInstaller from packaging/lumi.spec and the bundle policy gate
# (packaging/bundle-policy-linux.json). The packages come from
# packaging/linux_packages.py; the .rpm also needs rpmbuild, and the AppImage
# appimagetool and its runtime, downloaded below and checked against the
# SHA-256 GitHub published for them. Needs python3, pwsh, curl and rpmbuild.
#
# The bundle needs the glibc it was built with or newer: built on Ubuntu 22.04,
# that's glibc 2.35 (Ubuntu 22.04, Debian 12, Fedora 36 and later).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SBOM=""
if [[ "${1:-}" == "--sbom" ]]; then SBOM="$2"; fi

if [[ "$(uname)" != "Linux" ]]; then
  echo "build_linux.sh builds on Linux only" >&2
  exit 2
fi
if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "build_linux.sh builds x86_64 packages; this is $(uname -m)" >&2
  exit 2
fi

APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64"
RUNTIME_SHA256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

cd "$ROOT"
VERSION="$(python3 -c 'import re,pathlib;print(re.search(r"__version__\s*=\s*\"([^\"]+)\"", pathlib.Path("lumi/__init__.py").read_text()).group(1))')"
echo "Building Lumi $VERSION for Linux ($(uname -m), glibc $(ldd --version | head -1 | awk '{print $NF}'))"
rm -rf build dist/lumi lumi.egg-info

# Frontend libraries and fonts, pinned and SHA-256 verified.
pwsh -NoProfile -File packaging/fetch_web_assets.ps1 -Destination "$ROOT/lumi/gui/static/vendor"

WORK="$(mktemp -d)"
VENV="$WORK/venv"
python3 -m venv "$VENV"
PY="$VENV/bin/python"
"$PY" -m pip install --disable-pip-version-check --no-cache-dir --upgrade pip
"$PY" -m pip install --disable-pip-version-check --no-cache-dir --require-hashes -r packaging/requirements-release.txt
"$PY" -m pip install --disable-pip-version-check --no-cache-dir --no-deps "$ROOT"

NOTICES="$WORK/THIRD_PARTY_NOTICES.txt"
"$PY" packaging/third_party_notices.py --out "$NOTICES"
LUMI_THIRD_PARTY_NOTICES="$NOTICES" "$PY" -m PyInstaller packaging/lumi.spec --clean --noconfirm
test -x dist/lumi/lumi || { echo "PyInstaller did not produce dist/lumi/lumi" >&2; exit 1; }

"$PY" packaging/check_bundle.py dist/lumi --policy packaging/bundle-policy-linux.json \
  --manifest dist/bundle-manifest-linux.json

if [[ -n "$SBOM" ]]; then
  python3 -m cyclonedx_py environment --pyproject pyproject.toml --of JSON -o "$SBOM" "$PY"
  python3 packaging/third_party_notices.py --sbom "$SBOM" --validate
fi

# The same bundle gives the same packages.
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || date +%s)}"
mkdir -p dist/installer
python3 packaging/linux_packages.py deb --bundle dist/lumi --version "$VERSION" --out dist/installer
python3 packaging/linux_packages.py tarball --bundle dist/lumi --version "$VERSION" --out dist/installer

python3 packaging/linux_packages.py rpm --bundle dist/lumi --version "$VERSION" \
  --root "$WORK/rpm-root" --spec "$WORK/lumi.spec"
rpmbuild -bb --define "_topdir $WORK/rpmbuild" --define "_rpmdir $ROOT/dist/installer" \
  --define "_build_name_fmt %{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}.rpm" "$WORK/lumi.spec"

fetch() {  # url, file, sha256
  curl -fsSL --retry 3 -o "$2" "$1"
  echo "$3  $2" | sha256sum --check --strict -
}
fetch "$APPIMAGETOOL_URL" "$WORK/appimagetool" "$APPIMAGETOOL_SHA256"
fetch "$RUNTIME_URL" "$WORK/runtime-x86_64" "$RUNTIME_SHA256"
chmod +x "$WORK/appimagetool"
python3 packaging/linux_packages.py appdir --bundle dist/lumi --out "$WORK/Lumi.AppDir"
# --appimage-extract-and-run: appimagetool is an AppImage itself, and CI has no FUSE.
ARCH=x86_64 "$WORK/appimagetool" --appimage-extract-and-run --no-appstream \
  --runtime-file "$WORK/runtime-x86_64" "$WORK/Lumi.AppDir" "dist/installer/lumi-$VERSION-x86_64.AppImage"

ls -la dist/installer
