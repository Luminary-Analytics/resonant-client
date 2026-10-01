#!/usr/bin/env bash
# Build Lumi.app, lumi-X.Y.Z.dmg and lumi-X.Y.Z.pkg on macOS (docs/macos.md;
# the PKG is for device management, docs/deploy-macos.md).
#
#   packaging/build_macos.sh [--sbom dist/lumi-macos-sbom.cdx.json]
#
# Like scripts/build_clean.ps1 on Windows: a fresh virtual environment with
# the hash-pinned packaging/requirements-release.txt (which carries the
# macOS-only PyObjC wheels), the pinned web assets, third-party notices,
# PyInstaller from packaging/lumi.spec, and the bundle policy gate
# (packaging/bundle-policy-macos.json). Then Sparkle.framework, pinned and
# verified by packaging/fetch_sparkle.sh, goes into Lumi.app for automatic
# updates (lumi/sparkle.py).
#
# Signing and notarization run only when these are set (repository secrets
# in CI); otherwise the app is signed ad hoc, as Apple silicon requires, and
# the build says it isn't signed:
#   MACOS_SIGN_IDENTITY      "Developer ID Application: Luminary Analytics (TEAMID)"
#   MACOS_SIGN_P12_BASE64    the certificate and key, exported as .p12, base64
#   MACOS_SIGN_P12_PASSWORD  its password
#   MACOS_INSTALLER_IDENTITY "Developer ID Installer: Luminary Analytics (TEAMID)",
#                            in the same .p12, to sign the PKG
# and, for notarytool, either an App Store Connect API key:
#   APPLE_API_KEY_BASE64     the AuthKey_XXXX.p8 file, base64
#   APPLE_API_KEY_ID, APPLE_API_ISSUER_ID
# or an Apple ID with an app-specific password:
#   APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD
#
# MACOS_SIGNING_REQUIRED=true (a repository variable, set once the Developer ID
# exists) makes missing signing or notarization credentials fail the build:
# otherwise a lost secret would quietly ship an ad hoc build, which Sparkle
# installs over a notarized one (its EdDSA signature is still valid), and
# macOS then asks again for every privacy permission. The certificate file,
# the notary key and the temporary keychain are deleted when the script ends.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SBOM=""
if [[ "${1:-}" == "--sbom" ]]; then SBOM="$2"; fi

if [[ "$(uname)" != "Darwin" ]]; then
  echo "build_macos.sh builds on macOS only" >&2
  exit 2
fi

if [[ "${MACOS_SIGNING_REQUIRED:-}" == "true" ]]; then
  missing=()
  for name in MACOS_SIGN_IDENTITY MACOS_SIGN_P12_BASE64 MACOS_INSTALLER_IDENTITY; do
    [[ -n "${!name:-}" ]] || missing+=("$name")
  done
  if [[ -z "${APPLE_API_KEY_BASE64:-}" || -z "${APPLE_API_KEY_ID:-}" || -z "${APPLE_API_ISSUER_ID:-}" ]] &&
     [[ -z "${APPLE_ID:-}" || -z "${APPLE_TEAM_ID:-}" || -z "${APPLE_APP_PASSWORD:-}" ]]; then
    missing+=("APPLE_API_KEY_BASE64/APPLE_API_KEY_ID/APPLE_API_ISSUER_ID or APPLE_ID/APPLE_TEAM_ID/APPLE_APP_PASSWORD")
  fi
  if [[ ${#missing[@]} -gt 0 ]]; then
    echo "MACOS_SIGNING_REQUIRED is true, but these aren't set: ${missing[*]}; refusing to build an unsigned release" >&2
    exit 1
  fi
fi

cd "$ROOT"
VERSION="$(python3 -c 'import re,pathlib;print(re.search(r"__version__\s*=\s*\"([^\"]+)\"", pathlib.Path("lumi/__init__.py").read_text()).group(1))')"
echo "Building Lumi $VERSION for macOS ($(uname -m))"
rm -rf build dist/lumi dist/Lumi.app lumi.egg-info

# Frontend libraries and fonts, pinned and SHA-256 verified.
pwsh -NoProfile -File packaging/fetch_web_assets.ps1 -Destination "$ROOT/lumi/gui/static/vendor"
# Sparkle, pinned and SHA-256 verified; before the notices, which include its license.
bash packaging/fetch_sparkle.sh "$ROOT/packaging/sparkle"

VENV="$(mktemp -d)/venv"
WORK="$(dirname "$VENV")"
KEYCHAIN=""
cleanup() {
  # Signing material never outlives the build, whatever ended it.
  rm -f "$WORK/signing.p12" "$WORK/AuthKey.p8"
  if [[ -n "$KEYCHAIN" ]]; then security delete-keychain "$KEYCHAIN" 2>/dev/null || true; fi
  # Nor does the build's configuration: a checkout run from source has none.
  rm -f "$ROOT/lumi/_build_config.py"
}
trap cleanup EXIT
python3 -m venv "$VENV"
PY="$VENV/bin/python"
# What this build is made with: its feedback address, from LUMI_BUILD_FEEDBACK_URL
# (release.yml passes the repository variable LUMI_FEEDBACK_URL), before Lumi is
# installed and bundled. An address that isn't https, or carries a user name or
# password, stops the build.
"$PY" packaging/build_config.py
# Only hash-pinned packages: the pip that comes with this Python, the locked
# dependencies, then Lumi from this checkout with the pinned setuptools and no
# index, so nothing is resolved or downloaded again.
"$PY" -m pip install --disable-pip-version-check --no-cache-dir --require-hashes -r packaging/requirements-release.txt
"$PY" -m pip install --disable-pip-version-check --no-cache-dir --no-index --no-deps --no-build-isolation "$ROOT"

NOTICES="$WORK/THIRD_PARTY_NOTICES.txt"
"$PY" packaging/third_party_notices.py --out "$NOTICES"
LUMI_THIRD_PARTY_NOTICES="$NOTICES" "$PY" -m PyInstaller packaging/lumi.spec --clean --noconfirm
test -x dist/Lumi.app/Contents/MacOS/lumi || { echo "PyInstaller did not produce dist/Lumi.app" >&2; exit 1; }

"$PY" packaging/check_bundle.py dist/lumi --policy packaging/bundle-policy-macos.json \
  --manifest dist/bundle-manifest-macos.json

if [[ -n "$SBOM" ]]; then
  python3 -m cyclonedx_py environment --pyproject pyproject.toml --of JSON -o "$SBOM" "$PY"
  python3 packaging/third_party_notices.py --sbom "$SBOM" --validate
fi

# Sparkle goes where frameworks go, beside the libraries PyInstaller put in
# Contents/Frameworks; lumi/sparkle.py loads it from there. ditto keeps its symlinks.
SPARKLE="dist/Lumi.app/Contents/Frameworks/Sparkle.framework"
rm -rf "$SPARKLE"
ditto packaging/sparkle/Sparkle.framework "$SPARKLE"
for part in Sparkle Autoupdate Updater.app/Contents/MacOS/Updater Resources/Info.plist; do
  test -e "$SPARKLE/$part" || { echo "Lumi.app is missing Sparkle's $part" >&2; exit 1; }
done
if [[ -e "$SPARKLE/Versions/B/XPCServices" ]]; then
  echo "Sparkle's XPC services are for sandboxed apps and shouldn't ship" >&2
  exit 1
fi
for key in SUPublicEDKey SUFeedURL; do
  plutil -extract "$key" raw dist/Lumi.app/Contents/Info.plist >/dev/null \
    || { echo "Lumi.app's Info.plist has no $key" >&2; exit 1; }
done

# Sparkle's installer and progress app are signed on their own, without the
# entitlements Python needs; then the framework, then the app is sealed again
# (its signature records every nested one). The first pass signs everything
# PyInstaller collected.
sign_sparkle() {  # $@: the codesign options
  codesign --force "$@" "$SPARKLE/Versions/B/Autoupdate"
  codesign --force "$@" "$SPARKLE/Versions/B/Updater.app"
  codesign --force "$@" "$SPARKLE"
}

SIGNED=""
if [[ -n "${MACOS_SIGN_IDENTITY:-}" && -n "${MACOS_SIGN_P12_BASE64:-}" ]]; then
  KEYCHAIN="$WORK/signing.keychain-db"
  KEYCHAIN_PASSWORD="$(uuidgen)"
  echo "$MACOS_SIGN_P12_BASE64" | base64 --decode > "$WORK/signing.p12"
  security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
  security set-keychain-settings -lut 3600 "$KEYCHAIN"
  security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
  security import "$WORK/signing.p12" -k "$KEYCHAIN" -P "${MACOS_SIGN_P12_PASSWORD:-}" -T /usr/bin/codesign -T /usr/bin/productbuild
  rm -f "$WORK/signing.p12"
  security set-key-partition-list -S apple-tool:,apple: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN" >/dev/null
  security list-keychains -d user -s "$KEYCHAIN" $(security list-keychains -d user | tr -d '"')
  # Hardened runtime with the entitlements Python needs; every nested binary first.
  codesign --force --deep --options runtime --timestamp \
    --entitlements packaging/macos/entitlements.plist \
    --sign "$MACOS_SIGN_IDENTITY" dist/Lumi.app
  sign_sparkle --options runtime --timestamp --sign "$MACOS_SIGN_IDENTITY"
  codesign --force --options runtime --timestamp \
    --entitlements packaging/macos/entitlements.plist \
    --sign "$MACOS_SIGN_IDENTITY" dist/Lumi.app
  SIGNED=1
  echo "Signed with $MACOS_SIGN_IDENTITY"
else
  # Ad hoc, as PyInstaller left it: adding Sparkle changed the bundle, and
  # Sparkle only installs an update whose signature is intact.
  codesign --force --deep --sign - dist/Lumi.app
  echo "WARNING: MACOS_SIGN_IDENTITY isn't configured; Lumi.app is signed ad hoc, not with a Developer ID (see docs/macos.md)" >&2
fi
codesign --verify --deep --strict --verbose=2 dist/Lumi.app

# notarytool credentials: an App Store Connect API key, else an Apple ID.
NOTARY=()
if [[ -n "${APPLE_API_KEY_BASE64:-}" && -n "${APPLE_API_KEY_ID:-}" && -n "${APPLE_API_ISSUER_ID:-}" ]]; then
  echo "$APPLE_API_KEY_BASE64" | base64 --decode > "$WORK/AuthKey.p8"
  NOTARY=(--key "$WORK/AuthKey.p8" --key-id "$APPLE_API_KEY_ID" --issuer "$APPLE_API_ISSUER_ID")
elif [[ -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_PASSWORD:-}" ]]; then
  NOTARY=(--apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_PASSWORD")
fi

notarize() {  # $1: a signed DMG or PKG; submits it, waits, and staples the ticket
  local result status id
  result="$(xcrun notarytool submit "$1" "${NOTARY[@]}" --wait --output-format json)"
  echo "$result"
  status="$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("status", ""))')"
  if [[ "$status" != "Accepted" ]]; then
    id="$(printf '%s' "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id", ""))')"
    if [[ -n "$id" ]]; then xcrun notarytool log "$id" "${NOTARY[@]}" || true; fi
    echo "Notarizing $1 ended with status '$status'" >&2
    exit 1
  fi
  xcrun stapler staple "$1"
  echo "Notarized and stapled $1"
}

# Lumi's terms (packaging/legal_texts.py): the End User License Agreement, and
# for a pre-release the Alpha and Beta Test Terms. The PKG shows license.rtf on
# its license page. A disk image has no step to accept them in, so the DMG
# carries the texts beside the app; Lumi asks for them at first launch.
LEGAL="$WORK/legal"
python3 packaging/legal_texts.py rtf --out "$LEGAL" --version "$VERSION"

mkdir -p dist/installer
DMG="dist/installer/lumi-$VERSION.dmg"
rm -f "$DMG"
STAGE="$WORK/dmg"
mkdir -p "$STAGE"
cp -R dist/Lumi.app "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp "$LEGAL/eula.rtf" "$STAGE/License Agreement.rtf"
if [[ -f "$LEGAL/alpha-terms.rtf" ]]; then
  cp "$LEGAL/alpha-terms.rtf" "$STAGE/Alpha and Beta Test Terms.rtf"
fi
hdiutil create -volname "Lumi $VERSION" -srcfolder "$STAGE" -ov -format UDZO "$DMG"

# Sparkle's update is this disk image; the release signs its final bytes with
# EdDSA after this, so stapling comes first (.github/workflows/release.yml).
if [[ -n "$SIGNED" && ${#NOTARY[@]} -gt 0 ]]; then
  codesign --force --timestamp --sign "$MACOS_SIGN_IDENTITY" "$DMG"
  notarize "$DMG"
elif [[ -n "$SIGNED" ]]; then
  codesign --force --timestamp --sign "$MACOS_SIGN_IDENTITY" "$DMG"
  echo "WARNING: no notarytool credentials (APPLE_API_KEY_* or APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD); the DMG isn't notarized" >&2
fi
echo "DMG: $DMG ($(du -h "$DMG" | cut -f1))"

# The installer package for device management: the same app, marked as
# installed by the package so it leaves updates to the MDM
# (packaging/macos_pkg.py). The marker changes the bundle, so the staged copy
# is signed again: with the Developer ID when there is one, else ad hoc.
PKG_ROOT="$WORK/pkg-root"
PKG_WORK="$WORK/pkg-work"
mkdir -p "$PKG_ROOT/Applications" "$PKG_WORK"
ditto dist/Lumi.app "$PKG_ROOT/Applications/Lumi.app"
python3 packaging/macos_pkg.py marker "$PKG_ROOT/Applications/Lumi.app"
if [[ -n "$SIGNED" ]]; then
  codesign --force --options runtime --timestamp \
    --entitlements packaging/macos/entitlements.plist \
    --sign "$MACOS_SIGN_IDENTITY" "$PKG_ROOT/Applications/Lumi.app"
else
  codesign --force --deep --sign - "$PKG_ROOT/Applications/Lumi.app"
fi
codesign --verify --deep --strict --verbose=2 "$PKG_ROOT/Applications/Lumi.app"
pkgbuild --analyze --root "$PKG_ROOT" "$PKG_WORK/component.plist"
python3 packaging/macos_pkg.py component "$PKG_WORK/component.plist"
pkgbuild --root "$PKG_ROOT" --component-plist "$PKG_WORK/component.plist" \
  --identifier com.luminaryanalytics.lumi --version "$VERSION" --install-location / \
  "$PKG_WORK/lumi-component.pkg"
PKG_RESOURCES="$WORK/pkg-resources"
mkdir -p "$PKG_RESOURCES"
cp "$LEGAL/license.rtf" "$PKG_RESOURCES/license.rtf"
python3 packaging/macos_pkg.py distribution --version "$VERSION" --arch "$(uname -m)" \
  --license license.rtf --out "$PKG_WORK/distribution.xml"
PKG="dist/installer/lumi-$VERSION.pkg"
rm -f "$PKG"
if [[ -n "${MACOS_INSTALLER_IDENTITY:-}" && -n "${MACOS_SIGN_P12_BASE64:-}" ]]; then
  productbuild --distribution "$PKG_WORK/distribution.xml" --package-path "$PKG_WORK" \
    --resources "$PKG_RESOURCES" --sign "$MACOS_INSTALLER_IDENTITY" "$PKG"
  pkgutil --check-signature "$PKG"
  if [[ ${#NOTARY[@]} -gt 0 ]]; then
    notarize "$PKG"
  else
    echo "WARNING: no notarytool credentials; the PKG isn't notarized" >&2
  fi
else
  productbuild --distribution "$PKG_WORK/distribution.xml" --package-path "$PKG_WORK" \
    --resources "$PKG_RESOURCES" "$PKG"
  echo "WARNING: MACOS_INSTALLER_IDENTITY isn't configured; the PKG is unsigned (see docs/deploy-macos.md)" >&2
fi
echo "PKG: $PKG ($(du -h "$PKG" | cut -f1))"
