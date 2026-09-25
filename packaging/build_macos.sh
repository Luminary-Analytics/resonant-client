#!/usr/bin/env bash
# Build Lumi.app and lumi-X.Y.Z.dmg on macOS (docs/macos.md).
#
#   packaging/build_macos.sh [--sbom dist/lumi-macos-sbom.cdx.json]
#
# Like scripts/build_clean.ps1 on Windows: a fresh virtual environment with
# the hash-pinned packaging/requirements-release.txt (which carries the
# macOS-only PyObjC wheels), the pinned web assets, third-party notices,
# PyInstaller from packaging/lumi.spec, and the bundle policy gate
# (packaging/bundle-policy-macos.json).
#
# Signing and notarization run only when these are set (repository secrets
# in CI); otherwise the build is unsigned and says so:
#   MACOS_SIGN_IDENTITY      "Developer ID Application: Luminary Analytics (TEAMID)"
#   MACOS_SIGN_P12_BASE64    the certificate and key, exported as .p12, base64
#   MACOS_SIGN_P12_PASSWORD  its password
#   APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD   for notarytool
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SBOM=""
if [[ "${1:-}" == "--sbom" ]]; then SBOM="$2"; fi

if [[ "$(uname)" != "Darwin" ]]; then
  echo "build_macos.sh builds on macOS only" >&2
  exit 2
fi

cd "$ROOT"
VERSION="$(python3 -c 'import re,pathlib;print(re.search(r"__version__\s*=\s*\"([^\"]+)\"", pathlib.Path("lumi/__init__.py").read_text()).group(1))')"
echo "Building Lumi $VERSION for macOS ($(uname -m))"
rm -rf build dist/lumi dist/Lumi.app lumi.egg-info

# Frontend libraries and fonts, pinned and SHA-256 verified.
pwsh -NoProfile -File packaging/fetch_web_assets.ps1 -Destination "$ROOT/lumi/gui/static/vendor"

VENV="$(mktemp -d)/venv"
python3 -m venv "$VENV"
PY="$VENV/bin/python"
"$PY" -m pip install --disable-pip-version-check --no-cache-dir --upgrade pip
"$PY" -m pip install --disable-pip-version-check --no-cache-dir --require-hashes -r packaging/requirements-release.txt
"$PY" -m pip install --disable-pip-version-check --no-cache-dir --no-deps "$ROOT"

NOTICES="$(dirname "$VENV")/THIRD_PARTY_NOTICES.txt"
"$PY" packaging/third_party_notices.py --out "$NOTICES"
LUMI_THIRD_PARTY_NOTICES="$NOTICES" "$PY" -m PyInstaller packaging/lumi.spec --clean --noconfirm
test -x dist/Lumi.app/Contents/MacOS/lumi || { echo "PyInstaller did not produce dist/Lumi.app" >&2; exit 1; }

"$PY" packaging/check_bundle.py dist/lumi --policy packaging/bundle-policy-macos.json \
  --manifest dist/bundle-manifest-macos.json

if [[ -n "$SBOM" ]]; then
  python3 -m cyclonedx_py environment --pyproject pyproject.toml --of JSON -o "$SBOM" "$PY"
  python3 packaging/third_party_notices.py --sbom "$SBOM" --validate
fi

if [[ -n "${MACOS_SIGN_IDENTITY:-}" && -n "${MACOS_SIGN_P12_BASE64:-}" ]]; then
  KEYCHAIN="$(dirname "$VENV")/signing.keychain-db"
  KEYCHAIN_PASSWORD="$(uuidgen)"
  echo "$MACOS_SIGN_P12_BASE64" | base64 --decode > "$(dirname "$VENV")/signing.p12"
  security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
  security set-keychain-settings -lut 3600 "$KEYCHAIN"
  security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
  security import "$(dirname "$VENV")/signing.p12" -k "$KEYCHAIN" -P "${MACOS_SIGN_P12_PASSWORD:-}" -T /usr/bin/codesign
  security set-key-partition-list -S apple-tool:,apple: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN" >/dev/null
  security list-keychains -d user -s "$KEYCHAIN" $(security list-keychains -d user | tr -d '"')
  # Hardened runtime with the entitlements Python needs; every nested binary first.
  codesign --force --deep --options runtime --timestamp \
    --entitlements packaging/macos/entitlements.plist \
    --sign "$MACOS_SIGN_IDENTITY" dist/Lumi.app
  codesign --verify --deep --strict --verbose=2 dist/Lumi.app
  echo "Signed with $MACOS_SIGN_IDENTITY"
else
  echo "WARNING: MACOS_SIGN_IDENTITY isn't configured; Lumi.app is unsigned (see docs/macos.md)" >&2
fi

mkdir -p dist/installer
DMG="dist/installer/lumi-$VERSION.dmg"
rm -f "$DMG"
STAGE="$(dirname "$VENV")/dmg"
mkdir -p "$STAGE"
cp -R dist/Lumi.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -volname "Lumi $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG"

if [[ -n "${MACOS_SIGN_IDENTITY:-}" && -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_PASSWORD:-}" ]]; then
  codesign --force --timestamp --sign "$MACOS_SIGN_IDENTITY" "$DMG"
  xcrun notarytool submit "$DMG" --apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" \
    --password "$APPLE_APP_PASSWORD" --wait
  xcrun stapler staple "$DMG"
  echo "Notarized and stapled $DMG"
elif [[ -n "${MACOS_SIGN_IDENTITY:-}" ]]; then
  echo "WARNING: APPLE_ID, APPLE_TEAM_ID or APPLE_APP_PASSWORD isn't set; the DMG isn't notarized" >&2
fi
echo "DMG: $DMG ($(du -h "$DMG" | cut -f1))"
