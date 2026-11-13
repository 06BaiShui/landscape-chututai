@echo off
title ChuTai Render Panel  (http://127.0.0.1:8760)
cd /d "%~dp0"

rem ---- find a python that has Pillow ----
set "PY="
if defined CHUTAI_PYTHON if exist "%CHUTAI_PYTHON%" set "PY=%CHUTAI_PYTHON%"
if not defined PY if exist "%~dp0venv\Scripts\python.exe" set "PY=%~dp0venv\Scripts\python.exe"
if not defined PY if exist "D:\ComfyUI\.venv\Scripts\python.exe" set "PY=D:\ComfyUI\.venv\Scripts\python.exe"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython

echo.
echo   Render panel starting... open http://127.0.0.1:8760 in your browser.
echo   ComfyUI must be started by YOURSELF (port 8188). This script never
echo   starts or stops ComfyUI.
echo   Press Ctrl+C here to stop the panel.
echo.
"%PY%" "%~dp0panel.py"
echo.
echo   Panel exited.
pause >nul
exit /b 0

:nopython
echo.
echo   [X] Python not found.
echo       Install Python 3.10+ from https://www.python.org  (check "Add to PATH")
echo       then run:  pip install pillow
echo       Or set CHUTAI_PYTHON to your python.exe path.
echo.
pause
exit /b 1
