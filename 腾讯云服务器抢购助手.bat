@echo off

setlocal EnableExtensions
title Tencent Cloud Server Seckill Helper
pwsh -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
