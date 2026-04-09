@echo off
REM ═══════════════════════════════════════════════════════════
REM  ScanPerfect Nightly Refresh — Windows Task Scheduler
REM ═══════════════════════════════════════════════════════════
REM
REM  Schedule this in Task Scheduler:
REM    Trigger: Daily at 4:30 PM (weekdays only)
REM    Action:  Start a program
REM    Program: C:\path\to\swing-screener\nightly_refresh.bat
REM
REM  Setup instructions:
REM    1. Open Task Scheduler (taskschd.msc)
REM    2. Create Basic Task → name it "ScanPerfect Nightly"
REM    3. Trigger: Daily, start at 4:30 PM
REM    4. Action: Start a program
REM       Program/script: full path to this .bat file
REM       Start in: full path to the swing-screener repo folder
REM    5. In Properties → Conditions:
REM       - UNCHECK "Start only if computer is on AC power"
REM       - UNCHECK "Stop if computer switches to battery"
REM    6. In Properties → Settings:
REM       - CHECK "Run task as soon as possible after scheduled start is missed"
REM       - Set "Stop the task if it runs longer than" to 1 hour
REM
REM  The script skips weekends automatically. If the market was closed
REM  (holiday), Railway returns "already up to date" and it exits fast.
REM
REM  Logs go to local_runner\cache\nightly_log.txt
REM ═══════════════════════════════════════════════════════════

cd /d "%~dp0"

REM Force Python to use UTF-8 output (Windows cmd defaults to cp1252 which chokes on unicode)
set PYTHONIOENCODING=utf-8

REM Check if it's a weekend (0=Sunday, 6=Saturday in Python)
python -c "import datetime; d=datetime.datetime.now().weekday(); exit(0 if d<5 else 1)"
if errorlevel 1 (
    echo Skipping — weekend
    exit /b 0
)

echo ═══════════════════════════════════════════════════════════ >> local_runner\cache\nightly_log.txt
echo  Nightly refresh started: %date% %time% >> local_runner\cache\nightly_log.txt
echo ═══════════════════════════════════════════════════════════ >> local_runner\cache\nightly_log.txt

python local_runner\nightly.py >> local_runner\cache\nightly_log.txt 2>&1

echo  Nightly refresh complete: %date% %time% >> local_runner\cache\nightly_log.txt
echo. >> local_runner\cache\nightly_log.txt
