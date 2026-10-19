#!/bin/bash
# Build "Roadbook Builder.app" — a tiny launcher bundle that runs
# scripts/launch.sh (start server if needed, open default browser).
#
#   scripts/make_desktop_app.sh                 -> ~/Desktop/Roadbook Builder.app
#   scripts/make_desktop_app.sh /Applications   -> /Applications/Roadbook Builder.app
#
# The bundle points at this project folder by absolute path; re-run this
# script if you move the project.
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEST_DIR="${1:-$HOME/Desktop}"
APP="$DEST_DIR/Roadbook Builder.app"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

rm -rf "$APP"
chmod +x "$PROJECT_DIR/scripts/launch.sh"

# Built as an AppleScript applet, not a bare shell-script bundle: macOS
# privacy protection (TCC) blocks Finder-launched apps from reading
# ~/Documents, and only a "real" app like an applet gets the one-time
# "allow access to Documents folder" prompt — a script bundle just fails
# with "Operation not permitted".
osacompile -o "$APP" -e "do shell script quoted form of \"$PROJECT_DIR/scripts/launch.sh\" & \" >/dev/null 2>&1\""
plutil -replace CFBundleIdentifier -string org.endurancetrail.roadbook-builder "$APP/Contents/Info.plist"
plutil -replace CFBundleName -string "Roadbook Builder" "$APP/Contents/Info.plist"

# Icon: drawn with Pillow (already a pipeline dependency), then iconutil.
"$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/scripts/make_icon.py" "$TMP/icon_1024.png"
ICONSET="$TMP/AppIcon.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
    sips -z $s $s "$TMP/icon_1024.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
    sips -z $((s * 2)) $((s * 2)) "$TMP/icon_1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/applet.icns"
rm -f "$APP/Contents/Resources/Assets.car"   # newer osacompile ships its icon here; drop it so applet.icns wins
plutil -remove CFBundleIconName "$APP/Contents/Info.plist" 2>/dev/null || true

# resources changed after osacompile signed it -> re-sign (ad-hoc)
codesign --force --deep -s - "$APP" 2>/dev/null

touch "$APP"   # nudge Finder to refresh the icon
echo "Created $APP"
