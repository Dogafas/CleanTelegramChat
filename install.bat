@echo off
cd /d %~dp0
uv sync
if errorlevel 1 exit /b 1
echo Successfully installed requirements!
