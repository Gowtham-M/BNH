@echo off
REM ============================================================
REM  Bethune Nursing Home - Smart Attendance & Payroll
REM  One-file Windows build script (PyInstaller)
REM  Produces: dist\BethunePayroll.exe
REM ============================================================

setlocal
cd /d "%~dp0"

echo.
echo === [1/3] Creating/using virtual environment ===
if not exist ".venv" (
    python -m venv .venv
)
call .venv\Scripts\activate.bat

echo.
echo === [2/3] Installing dependencies ===
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo.
echo === [3/3] Building BethunePayroll.exe ===
pyinstaller ^
  --noconfirm ^
  --clean ^
  --onefile ^
  --windowed ^
  --name BethunePayroll ^
  --add-data "ui;ui" ^
  --hidden-import pyodbc ^
  --collect-all webview ^
  main.py

echo.
if exist "dist\BethunePayroll.exe" (
    echo BUILD SUCCESSFUL -^> dist\BethunePayroll.exe
) else (
    echo BUILD FAILED - check the PyInstaller output above.
)
echo.
pause
endlocal
