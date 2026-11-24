#!/usr/bin/env pythonw
"""Silent Windows System Tray launcher for Offset (double-click, no console window)."""
import sys
from run import main

if __name__ == "__main__":
    if "--tray" not in sys.argv:
        sys.argv.append("--tray")
    if "--open" not in sys.argv:
        sys.argv.append("--open")
    raise SystemExit(main())
