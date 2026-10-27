@echo off
setlocal
set "ROOT=%~dp0"
if exist "%ROOT%.venv\Scripts\python.exe" (
    "%ROOT%.venv\Scripts\python.exe" "%ROOT%meetpoint_debug.py" %*
) else (
    python "%ROOT%meetpoint_debug.py" %*
)
exit /b %ERRORLEVEL%
