@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\service.ps1" %*
if "%~1"=="" pause
