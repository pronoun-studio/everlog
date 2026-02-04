"""Compatibility shim for `everytimecapture.cli` → `everlog.cli`."""

from everlog.cli import main


if __name__ == "__main__":
    raise SystemExit(main())

