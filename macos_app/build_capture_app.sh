#!/usr/bin/env bash
set -euo pipefail

# Builds a minimal macOS .app wrapper without external dependencies.
# This is useful when you cannot grant Screen Recording permission to a raw Python binary.
#
# Output:
#   macos_app/dist/everlog-capture.app
#
# Usage:
#   ./macos_app/build_capture_app.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
DIST_DIR="${ROOT_DIR}/macos_app/dist"
APP_PATH="${DIST_DIR}/everlog-capture.app"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Python not found: ${PYTHON_BIN}" >&2
  echo "Create venv first: python3 -m venv .venv && ./.venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

mkdir -p "${DIST_DIR}"

TMP_SCRIPT="$(mktemp -t everlog_capture.XXXXXX.js)"
trap 'rm -f "${TMP_SCRIPT}"' EXIT

cat > "${TMP_SCRIPT}" <<EOF
function _shQuote(s) {
  // Wrap in single quotes and escape existing single quotes.
  return "'" + String(s).replace(/'/g, "'\\\\''") + "'";
}

function run(argv) {
  var app = Application.currentApplication();
  app.includeStandardAdditions = true;
  try {
    var args = (argv && argv.length) ? argv : ["capture"];
    var argsStr = args.map(_shQuote).join(" ");
    app.doShellScript("cd '${ROOT_DIR}' && '${PYTHON_BIN}' -m everlog.cli " + argsStr);
  } catch (e) {
    app.displayDialog("Capture failed: " + e.toString(), {buttons: ["OK"], defaultButton: "OK"});
  }
}
EOF

rm -rf "${APP_PATH}"
/usr/bin/osacompile -l JavaScript -o "${APP_PATH}" "${TMP_SCRIPT}"

# Set custom app icon if available
ICON_FILE="${ROOT_DIR}/macos_app/Everlog.icns"
if [[ -f "${ICON_FILE}" ]]; then
  cp "${ICON_FILE}" "${APP_PATH}/Contents/Resources/applet.icns"
  /usr/libexec/PlistBuddy -c "Set :CFBundleIconFile applet" "${APP_PATH}/Contents/Info.plist"
fi

# Touch to update Finder icon cache
touch "${APP_PATH}"

echo "Built: ${APP_PATH}"
