#!/usr/bin/env bash
# Fetch the pinned Sparkle.framework for Lumi.app's automatic updates (lumi/sparkle.py).
#
#   packaging/fetch_sparkle.sh [destination]      # default: packaging/sparkle
#
# Like packaging/fetch_ripgrep.ps1: downloaded at build time rather than
# committed, from the release on github.com/sparkle-project/Sparkle, and
# checked against the SHA-256 below BEFORE anything is extracted. A mismatch
# fails the build; nothing unverified reaches the app.
#
# The archive is kept beside the framework so a rebuild needn't download it
# again, but it is checked again on every run, and the framework is always
# extracted afresh from it: a framework left from an earlier run is never
# used as it is.
#
# Leaves <destination>/Sparkle.framework and <destination>/LICENSE (Sparkle is
# MIT with bundled BSD, zlib and MIT parts; packaging/third-party-components.json
# ships the text in THIRD_PARTY_NOTICES.txt). The XPC services are removed:
# they serve sandboxed apps only, and Lumi isn't one (SUEnableInstallerLauncherService
# and SUEnableDownloaderService stay unset).
#
# To upgrade: change both values, taking the hash from the release asset's
# digest (`gh api repos/sparkle-project/Sparkle/releases/tags/<version>`),
# never from a local download alone, and update the version in
# packaging/third-party-components.json.
set -euo pipefail

SPARKLE_VERSION="2.10.0"
SPARKLE_SHA256="c2bf58aa8387266ac179357b1415d6f2635f044da8be41042af32425dae6da0c"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$ROOT/packaging/sparkle}"
ARCHIVE="Sparkle-$SPARKLE_VERSION.tar.xz"
URL="https://github.com/sparkle-project/Sparkle/releases/download/$SPARKLE_VERSION/$ARCHIVE"
KEPT="$DEST/$ARCHIVE"

sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$DEST"
if [[ -f "$KEPT" && "$(sha256 "$KEPT")" == "$SPARKLE_SHA256" ]]; then
  cp "$KEPT" "$WORK/$ARCHIVE"
  echo "Sparkle $SPARKLE_VERSION: the kept archive's SHA-256 matches"
else
  rm -f "$KEPT"
  curl -fsSL --retry 3 -o "$WORK/$ARCHIVE" "$URL"
fi
ACTUAL="$(sha256 "$WORK/$ARCHIVE")"
if [[ "$ACTUAL" != "$SPARKLE_SHA256" ]]; then
  echo "Sparkle $SPARKLE_VERSION: SHA-256 mismatch (expected $SPARKLE_SHA256, got $ACTUAL)" >&2
  exit 1
fi

mkdir -p "$WORK/x"
tar -xJf "$WORK/$ARCHIVE" -C "$WORK/x" ./Sparkle.framework ./LICENSE
rm -rf "$WORK/x/Sparkle.framework/Versions/B/XPCServices" "$WORK/x/Sparkle.framework/XPCServices"
rm -rf "$DEST/Sparkle.framework" "$DEST/LICENSE"
# ditto keeps the framework's symlinks (Versions/Current and the top-level links).
ditto "$WORK/x/Sparkle.framework" "$DEST/Sparkle.framework"
cp "$WORK/x/LICENSE" "$DEST/LICENSE"
cp "$WORK/$ARCHIVE" "$KEPT"
installed="$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" \
  "$DEST/Sparkle.framework/Versions/B/Resources/Info.plist" 2>/dev/null || true)"
if [[ "$installed" != "$SPARKLE_VERSION" ]]; then
  echo "Sparkle.framework in $DEST isn't version $SPARKLE_VERSION" >&2
  exit 1
fi
echo "Sparkle $SPARKLE_VERSION (sha256 $ACTUAL) in $DEST"
