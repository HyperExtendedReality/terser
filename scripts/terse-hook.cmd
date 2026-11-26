@echo off
setlocal
set "PLUGIN_ROOT=%~dp0.."
set "HOOK_PY=%PLUGIN_ROOT%\runtime\unified_hook.py"
set "FAST_BIN=%PLUGIN_ROOT%\bin\terser-fast.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PY_BIN="

if defined TERSER_PYTHON (
  if exist "%TERSER_PYTHON%" set "PY_BIN=%TERSER_PYTHON%"
)

if not defined PY_BIN (
  for %%P in (pythonw.exe python.exe) do (
    for /f "delims=" %%F in ('where %%P 2^>nul') do (
      echo %%F | findstr /I /C:"WindowsApps" >nul
      if errorlevel 1 if not defined PY_BIN set "PY_BIN=%%F"
    )
  )
)

if exist "%FAST_BIN%" (
  if defined PY_BIN (
    "%FAST_BIN%" --python "%PY_BIN%" --script "%HOOK_PY%" %*
    exit /b 0
  ) else (
    "%FAST_BIN%" %*
    exit /b 0
  )
)

if defined PY_BIN (
  "%PY_BIN%" -E -s -X utf8 "%HOOK_PY%" %*
  exit /b 0
)

where py.exe >nul 2>&1
if not errorlevel 1 (
  py -3 -E -s -X utf8 "%HOOK_PY%" %*
  exit /b 0
)

exit /b 0
