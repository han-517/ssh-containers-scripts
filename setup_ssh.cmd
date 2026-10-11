@echo off
setlocal
rem Windows CMD launcher. Keep setup_ssh.ps1 beside this file.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_ssh.ps1" %*
exit /b %errorlevel%
