@echo off
cd /d "%~dp0"
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo FFmpeg no esta en PATH. Lee README.md antes de iniciar.
  pause
  exit /b 1
)
py -3 run.py
pause
