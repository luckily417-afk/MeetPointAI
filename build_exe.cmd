@echo off
REM Wrapper for build_exe.ps1: bypasses the default PowerShell execution policy.
REM Usage: build_exe.cmd              (run tests, then build)
REM        build_exe.cmd -SkipTests   (build only)
setlocal
set "ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%build_exe.ps1" %*
exit /b %ERRORLEVEL%
