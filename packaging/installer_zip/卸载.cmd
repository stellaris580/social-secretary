@echo off
title AI 社交秘书 卸载程序
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0卸载.ps1" %*
echo.
pause
