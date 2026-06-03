@echo off
REM Daily All Tracker — scheduled wrapper invoked by Windows Task Scheduler.
REM Runs daily_all.py from the project directory and logs output for debugging.

REM Move to project root (this batch file's directory)
cd /d "%~dp0"

REM Create log dir if needed
if not exist "tmp\scheduler" mkdir "tmp\scheduler"

REM Timestamp for log filename
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value 2^>nul') do set DT=%%I
set LOG=tmp\scheduler\daily_all_%DT:~0,8%_%DT:~8,6%.log

REM Run with full Python path for safety, capture stdout+stderr
echo === MLB Daily All Tracker started %DATE% %TIME% === > "%LOG%"
"C:\Users\17146\AppData\Local\Programs\Python\Python313\python.exe" daily_all.py >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo === Finished %DATE% %TIME% (exit %RC%) === >> "%LOG%"

REM Also write a "latest" symlink-like copy for easy access
copy /Y "%LOG%" "tmp\scheduler\latest.log" >nul

exit /b %RC%
