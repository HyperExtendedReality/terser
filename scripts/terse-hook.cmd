@echo off
setlocal
set "PLUGIN_ROOT=%~dp0.."
set "HOOK_PY=%PLUGIN_ROOT%\runtime\unified_hook.py"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if defined TERSER_PYTHON (
  if exist "%TERSER_PYTHON%" (
    "%TERSER_PYTHON%" -E -s -X utf8 "%HOOK_PY%" %*
    exit /b 0
  )
)

for %%P in (python.exe) do (
  for /f "delims=" %%F in ('where %%P 2^>nul') do (
    echo %%F | findstr /I /C:"WindowsApps" >nul
    if errorlevel 1 (
      "%%F" -E -s -X utf8 "%HOOK_PY%" %*
      exit /b 0
    )
  )
)

where py.exe >nul 2>&1
if not errorlevel 1 (
  py -3 -E -s -X utf8 "%HOOK_PY%" %*
  exit /b 0
)

exit /b 0
