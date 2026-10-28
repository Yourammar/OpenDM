#!/usr/bin/env bash
# Builds OpenDM-Setup-<version>.exe on Linux with NSIS (makensis).
# Needs: a Windows Python runtime folder (python + tk + app packages, FFmpeg, Deno),
# passed as RUNTIME=/path/to/runtime. See README "Building the installer".
set -euo pipefail
VERSION="${VERSION:-1.2.0}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
RUNTIME="${RUNTIME:?set RUNTIME to the Windows runtime folder}"
STAGE="${STAGE:-$HERE/stage}"
OUT="${OUT:-$HERE/dist}"

rm -rf "$STAGE" && mkdir -p "$STAGE/app" "$OUT"
cp "$ROOT/app/OpenDM.pyw" "$STAGE/app/"
cp -r "$ROOT/extension" "$STAGE/extension"
cp "$ROOT/LICENSE" "$ROOT/README.md" "$HERE/extension-setup.html" "$HERE/opendm.ico" "$STAGE/"
cp -r "$RUNTIME" "$STAGE/runtime"
find "$STAGE" -name "__pycache__" -type d -prune -exec rm -rf {} +

cd "$HERE"
makensis -V2 -DVERSION="$VERSION" -DOUTFILE="$STAGE/OpenDM.exe" launcher.nsi
makensis -V2 -DVERSION="$VERSION" -DSTAGE="$STAGE" -DOUTFILE="$OUT/OpenDM-Setup-$VERSION.exe" installer.nsi
ls -la "$OUT"
