@echo off
setlocal
set "PLUGIN_ROOT=%~dp0.."
set "HOOK_PY=%PLUGIN_ROOT%\runtime\unified_hook.py"
set "FAST_BIN=%PLUGIN_ROOT%\bin\terser-fast.exe"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if exist "%FAST_BIN%" (
  if defined TERSER_PYTHON (
    if exist "%TERSER_PYTHON%" (
      "%FAST_BIN%" --python "%TERSER_PYTHON%" --script "%HOOK_PY%" %*
      exit /b 0
    )
  )
  "%FAST_BIN%" --script "%HOOK_PY%" %*
  exit /b 0
)

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
