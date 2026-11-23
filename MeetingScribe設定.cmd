@echo off
rem Double-click to open the MeetingScribe settings window (API keys, engine region, prices).
if not exist "%~dp0.venv\Scripts\pythonw.exe" (
  echo .venv not found. Please run scripts\setup.ps1 first.
  pause
  exit /b 1
)
start "" "%~dp0.venv\Scripts\pythonw.exe" -m meeting_scribe
