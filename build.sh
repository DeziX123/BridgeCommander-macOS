#!/bin/zsh
set -euo pipefail
cd "${0:A:h}"
if [[ ! -d .venv ]]; then
  python3.11 -m venv .venv
fi
.venv/bin/python -m pip install -r requirements.txt
mkdir -p build/icon.iconset
sips -z 1024 1024 assets/icon.png --out build/icon-1024.png >/dev/null
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" build/icon-1024.png --out "build/icon.iconset/icon_${size}x${size}.png" >/dev/null
  retina=$((size * 2))
  sips -z "$retina" "$retina" build/icon-1024.png --out "build/icon.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns build/icon.iconset -o build/icon.icns
.venv/bin/pyinstaller --noconfirm --clean --windowed --name "Bridge Commander" \
  --icon "$PWD/build/icon.icns" --osx-bundle-identifier com.bridgecommander.macos \
  --add-data "$PWD/assets/icons:assets/icons" \
  --hidden-import keyring.backends.macOS \
  --exclude-module tkinter --exclude-module matplotlib --exclude-module IPython \
  --distpath dist --workpath build/pyinstaller --specpath build main.py
plutil -replace CFBundleShortVersionString -string 0.1.0 "dist/Bridge Commander.app/Contents/Info.plist"
plutil -replace CFBundleVersion -string 1 "dist/Bridge Commander.app/Contents/Info.plist"
codesign --force --deep --sign - "dist/Bridge Commander.app"
codesign --verify --deep --strict "dist/Bridge Commander.app"
echo "Built dist/Bridge Commander.app"
