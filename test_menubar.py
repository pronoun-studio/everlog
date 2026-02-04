#!/usr/bin/env python3
"""Simple test script for rumps menubar."""
import sys
print(f"Python: {sys.executable}", flush=True)

try:
    import rumps
    print(f"rumps version: {rumps.__version__}", flush=True)
except ImportError as e:
    print(f"ERROR: Cannot import rumps: {e}", flush=True)
    sys.exit(1)

class TestApp(rumps.App):
    def __init__(self):
        super().__init__("TEST")
        self.menu = [
            rumps.MenuItem("Hello", callback=lambda _: print("Hello clicked!")),
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]
        print("TestApp initialized!", flush=True)

if __name__ == "__main__":
    print("Starting TestApp...", flush=True)
    TestApp().run()
