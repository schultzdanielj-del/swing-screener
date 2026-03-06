@echo off
cd /d C:\Users\Dan\Documents\ScanPerfect\swing-screener
echo Pulling latest code...
git pull
echo.
echo Starting agent...
python local_runner/agent.py
pause
