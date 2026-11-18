@echo off
cd /d "%~dp0"
echo ===== Python environment check =====
echo.
echo [1] python location:
python -c "import sys; print(sys.executable); print(sys.version)"
echo.
echo [2] dependencies:
python -c "import win32gui; print('pywin32: OK')" 2>&1
python -c "import PIL; print('pillow: OK')" 2>&1
echo.
echo [3] test launch (3s selftest):
python radial_click.py --selftest
echo.
echo ===== done =====
pause
