@echo off
REM Double-click this file to start the RapidFlow AI receptionist.
REM Requires ANTHROPIC_API_KEY to be set permanently (see README).
cd /d "%~dp0"
echo Starting RapidFlow receptionist...
echo Open your browser to  http://localhost:5000
echo (Close this window or press Ctrl+C to stop.)
echo.
python server.py
pause
