@echo off
rem Starts the nrf24-sniffer web UI. Double-click, or run from a terminal.
rem
rem Uses the project's own virtualenv on purpose: bthome-ble lives there, and
rem the system Python would bring the BTHome decoder up as "unavailable".

rem Run from this script's own folder, wherever it was launched from.
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
  echo The virtualenv is missing. Create it once with:
  echo.
  echo     python -m venv .venv
  echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

rem No --no-browser: opening the page is the point of double-clicking this.
"%PY%" nrf24web.py %*

rem Keep the window open if it exited on its own (a crash, a busy port), so the
rem message is readable instead of vanishing with the window.
if errorlevel 1 pause
