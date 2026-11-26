@echo off
setlocal
set "PLUGIN_ROOT=%~dp0.."
set "CLI_PY=%PLUGIN_ROOT%\runtime\cli.py"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if defined TERSER_PYTHON (
  if exist "%TERSER_PYTHON%" (
    "%TERSER_PYTHON%" -X utf8 "%CLI_PY%" %*
    exit /b %ERRORLEVEL%
  )
)

for %%P in (python.exe) do (
  for /f "delims=" %%F in ('where %%P 2^>nul') do (
    echo %%F | findstr /I /C:"WindowsApps" >nul
    if errorlevel 1 (
      "%%F" -X utf8 "%CLI_PY%" %*
      exit /b %ERRORLEVEL%
    )
  )
)

where py.exe >nul 2>&1
if not errorlevel 1 (
  py -3 -X utf8 "%CLI_PY%" %*
  exit /b %ERRORLEVEL%
)

echo Terser: python not found on PATH. 1>&2
exit /b 1
