"""
macOS app entrypoint (py2app).

Launches the menubar app by default.
You can also pass other commands via:
  open -a Everlog.app --args capture
"""

from __future__ import annotations

import sys


def main() -> int:
    # Default to menubar if no command is provided
    if len(sys.argv) == 1:
        sys.argv.append("menubar")

    from everlog.cli import main as cli_main

    return int(cli_main())


if __name__ == "__main__":
    raise SystemExit(main())

