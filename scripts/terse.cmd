@echo off
setlocal
set "PLUGIN_ROOT=%~dp0.."
set "CLI_PY=%PLUGIN_ROOT%\runtime\cli.py"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

if defined TERSER_PYTHON (
  if exist "%TERSER_PYTHON%" (
    set "TERSER_CLI_PYTHON=%TERSER_PYTHON%"
    goto run_python
  )
)

for %%P in (python.exe) do (
  for /f "delims=" %%F in ('where %%P 2^>nul') do (
    echo %%F | findstr /I /C:"WindowsApps" >nul
    if errorlevel 1 (
      set "TERSER_CLI_PYTHON=%%F"
      goto run_python
    )
  )
)

where py.exe >nul 2>&1
if errorlevel 1 goto missing_python
py -3 -X utf8 "%CLI_PY%" %*
exit /b %ERRORLEVEL%

:run_python
"%TERSER_CLI_PYTHON%" -X utf8 "%CLI_PY%" %*
exit /b %ERRORLEVEL%

:missing_python
echo Terser: python not found on PATH. 1>&2
exit /b 1
