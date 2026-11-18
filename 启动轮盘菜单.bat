@echo off
cd /d "%~dp0"
python -c "import win32gui, win32api, win32con, pythoncom, PIL" >nul 2>&1
if errorlevel 1 (
  echo [SETUP] installing pywin32 and pillow, please wait...
  python -m pip install pywin32 pillow
)

python -c "import win32gui, PIL" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python dependencies not ready.
  echo Please run manually:  python -m pip install pywin32 pillow
  echo Then try again. Press any key to exit.
  pause
  exit /b 1
)

echo [OK] Starting radial menu. Keep this window open.
echo Quit: right click tray icon, or Ctrl+Alt+Shift+Q
python radial_click.py

echo.
echo [EXIT] program stopped.
pause
