@echo off
title AI 社交秘书 安装程序
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0安装.ps1" %*
echo.
pause
