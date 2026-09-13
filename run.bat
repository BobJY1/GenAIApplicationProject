@echo off
REM Pilot run, end to end. Usage:  run.bat 50
setlocal
if "%ANTHROPIC_API_KEY%"=="" (echo Set ANTHROPIC_API_KEY first & exit /b 1)
set LIMIT=%1
if "%LIMIT%"=="" set LIMIT=50

echo == 0. offline checks (no API calls, no cost) ==
python omml.py             || exit /b 1
python verifier.py         || exit /b 1
python smoke_test.py       || exit /b 1

echo. & echo == 1. read the boards ==
python pipeline.py extract --limit %LIMIT%   || exit /b 1

echo. & echo == 2. review the statements BEFORE paying to solve them ==
python pipeline.py index    || exit /b 1
python pipeline.py review   || exit /b 1
echo    Open output\review\review.html, work the queue, click Download decisions,
echo    then:  python review.py apply %%USERPROFILE%%\Downloads\decisions.json
echo    Press any key to continue without reviewing, or Ctrl-C to stop here.
pause >nul

echo. & echo == 3. solve and verify ==
python pipeline.py solve    || exit /b 1
echo. & echo == 4. build the questions ==
python pipeline.py mcq      || exit /b 1
echo. & echo == 5. check and build ==
python pipeline.py qa       || exit /b 1
python pipeline.py build || exit /b 1
python pipeline.py index    || exit /b 1
python db.py stats
