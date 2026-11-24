#!/usr/bin/env python3
"""Build a standalone Windows Offset.exe portable package using PyInstaller.

Usage:
  python build_exe.py               # Builds dist/Offset/Offset.exe + bundles ffmpeg/ffprobe if found
  python build_exe.py --no-ffmpeg   # Builds Offset.exe without bundling ffmpeg binaries
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

from offset.icon import ensure_icon_file
from offset.paths import resolve_binary


def main():
    parser = argparse.ArgumentParser(description="Empaqueta Offset como Offset.exe para Windows.")
    parser.add_argument("--no-ffmpeg", action="store_true", help="No copiar ffmpeg.exe/ffprobe.exe dentro de dist/Offset/bin.")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent
    ico = ensure_icon_file(root / "web" / "offset.ico")
    print(f"Icono generado/verificado: {ico}")

    check = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"], capture_output=True)
    if check.returncode != 0:
        print("PyInstaller no está instalado en este Python.", file=sys.stderr)
        print("Instálalo con:  python -m pip install pyinstaller", file=sys.stderr)
        return 1

    sep = ";" if os.name == "nt" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", "Offset",
        "--windowed",
        "--icon", str(ico),
        "--add-data", f"{root / 'web'}{sep}web",
        str(root / "run.py"),
    ]
    print("Ejecutando:", " ".join(cmd))
    res = subprocess.run(cmd, cwd=str(root))
    if res.returncode != 0:
        return res.returncode

    dist_dir = root / "dist" / "Offset"
    if not args.no_ffmpeg:
        bin_dir = dist_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for tool in ("ffmpeg", "ffprobe"):
            found = resolve_binary(tool)
            src = Path(found)
            if src.is_file():
                dst = bin_dir / src.name
                shutil.copy2(src, dst)
                print(f"Empaquetado binario: {dst}")
            else:
                print(f"Aviso: no se encontró {tool} para empaquetar; se usará PATH del sistema.")

    print(f"\nListo: {dist_dir / 'Offset.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
