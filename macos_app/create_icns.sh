#!/bin/bash
# PNG to ICNS converter for macOS app icon
# Usage: ./create_icns.sh icon.png

set -e

INPUT="${1:-icon.png}"
OUTPUT="Everlog.icns"
ICONSET="Everlog.iconset"

if [ ! -f "$INPUT" ]; then
    echo "Error: $INPUT not found"
    exit 1
fi

# Clean up any existing iconset
rm -rf "$ICONSET"
mkdir "$ICONSET"

# Generate all required sizes
sips -z 16 16     "$INPUT" --out "$ICONSET/icon_16x16.png"
sips -z 32 32     "$INPUT" --out "$ICONSET/icon_16x16@2x.png"
sips -z 32 32     "$INPUT" --out "$ICONSET/icon_32x32.png"
sips -z 64 64     "$INPUT" --out "$ICONSET/icon_32x32@2x.png"
sips -z 128 128   "$INPUT" --out "$ICONSET/icon_128x128.png"
sips -z 256 256   "$INPUT" --out "$ICONSET/icon_128x128@2x.png"
sips -z 256 256   "$INPUT" --out "$ICONSET/icon_256x256.png"
sips -z 512 512   "$INPUT" --out "$ICONSET/icon_256x256@2x.png"
sips -z 512 512   "$INPUT" --out "$ICONSET/icon_512x512.png"
sips -z 1024 1024 "$INPUT" --out "$ICONSET/icon_512x512@2x.png"

# Convert to icns
iconutil -c icns "$ICONSET" -o "$OUTPUT"

# Clean up
rm -rf "$ICONSET"

echo "✅ Created $OUTPUT"
