#!/bin/bash
# Builds "Surya Memory.app" into dist/. Needs the Xcode command line tools.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
app="$root/dist/Surya Memory.app"
version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$root/surya_memory/__init__.py")"

rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/scripts" "$app/Contents/Resources/surya_memory"

swiftc -O -framework Cocoa -framework WebKit -o "$app/Contents/MacOS/SuryaMemory" "$root/app/macos/main.swift"

# The app carries its own copy of the server and the sync scripts.
cp "$root"/surya_memory/*.py "$root"/surya_memory/app.html "$app/Contents/Resources/surya_memory/"
cp "$root"/scripts/* "$app/Contents/Resources/scripts/"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
"$app/Contents/MacOS/SuryaMemory" --make-icon "$work/icon.iconset"
iconutil -c icns -o "$app/Contents/Resources/icon.icns" "$work/icon.iconset"

cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Surya Memory</string>
  <key>CFBundleDisplayName</key><string>Surya Memory</string>
  <key>CFBundleIdentifier</key><string>io.github.krishantm.surya-memory</string>
  <key>CFBundleExecutable</key><string>SuryaMemory</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$version</string>
  <key>CFBundleVersion</key><string>$version</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST

codesign --force --sign - "$app" 2>/dev/null || true
echo "Built $app"
