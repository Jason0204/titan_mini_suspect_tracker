@echo off
setlocal
cd /d "%~dp0"

rem Changed 2026-10-18: look for .venv / ui_venv / venv instead of hardcoding ui_venv.
set "VENV_PY="
for %%D in (.venv ui_venv venv) do (
    if not defined VENV_PY if exist "%%D\Scripts\pythonw.exe" set "VENV_PY=%CD%\%%D\Scripts\pythonw.exe"
)

if not defined VENV_PY (
    echo Python environment not found: expected .venv, ui_venv or venv under this folder.
    echo Create one with:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

set "PYTHONPATH=%CD%\src"
start "Titan Suspect Tracker" "%VENV_PY%" -m suspect_tracker
