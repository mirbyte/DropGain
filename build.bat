@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Create it first:
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-build.txt
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" "%~dp0build.py" %*
set EXITCODE=%ERRORLEVEL%

echo.
if %EXITCODE% neq 0 (
  echo Build failed with exit code %EXITCODE%.
) else (
  echo Build finished.
)
pause
exit /b %EXITCODE%
