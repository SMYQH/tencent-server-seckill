@echo off
rem No `chcp` here on purpose: on Windows, CPython writes console output through
rem the wide-char API, so Chinese renders correctly whatever the console codepage
rem is. `chcp` would additionally swallow a redirected stdin, which breaks
rem `助手.bat < answers.txt` style automation.
rem
rem This file is UTF-8, so every echo/title line below stays pure ASCII:
rem cmd prints its own text using the console codepage, and Chinese here
rem would show as mojibake on a CP936 or CP437 console. All Chinese UI text
rem is printed by Python instead, which writes through the wide-char console
rem API and is codepage-independent.
setlocal EnableExtensions
title Tencent Cloud Server Seckill Helper
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
