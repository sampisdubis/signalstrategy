@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Nedostaje .venv\Scripts\pythonw.exe
  echo Pokreni prvo: python -m venv .venv
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "dashboard.py"
exit /b 0
