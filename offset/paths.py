"""Runtime path helpers (works both from source and frozen PyInstaller .exe)."""
import os
from pathlib import Path
import shutil
import sys


def app_root() -> Path:
    """Return directory containing bundled read-only assets (web/)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def data_root() -> Path:
    """Return writable directory for local data (settings.json, custom slate)."""
    if getattr(sys, "frozen", False):
        if os.name == "nt":
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
            return base / "Offset" / "data"
        return Path.home() / ".local" / "share" / "offset" / "data"
    return Path(__file__).resolve().parent.parent / "data"


def resolve_binary(name: str) -> str:
    """Locate ffmpeg/ffprobe next to Offset.exe, in bundled bin/, or on PATH."""
    exe_name = f"{name}.exe" if os.name == "nt" and not name.lower().endswith(".exe") else name
    candidates = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates += [exe_dir / exe_name, exe_dir / "bin" / exe_name]
        if hasattr(sys, "_MEIPASS"):
            mp = Path(sys._MEIPASS)
            candidates += [mp / exe_name, mp / "bin" / exe_name]
    else:
        root = Path(__file__).resolve().parent.parent
        candidates += [root / "bin" / exe_name]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    found = shutil.which(name)
    return found or name
