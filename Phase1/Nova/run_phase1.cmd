@echo off
rem run_phase1.cmd: the day's zip from AB -> the Historical tick files,
rem limitUpDown.csv and TradingData.csv, one R job after the other.
rem
rem     run_phase1.cmd C:\path\to\phase1-YYYYMMDD.zip
rem     run_phase1.cmd C:\path\to\phase1-YYYYMMDD.zip "Test|Prod"
rem
rem The second argument, quoted, is the environments limitUpDown.csv is
rem copied to; without it, all three. Stops at the first job that fails.
setlocal

rem R 3.2.2's Rscript on this machine.
set "RSCRIPT=C:\path\to\R-3.2.2\bin\x64\Rscript.exe"

set "HERE=%~dp0"
set "ZIP=%~1"
set "ENVS=%~2"

rem No ( ) blocks below: a path with a ) in it would end one early.
if "%ZIP%"=="" goto usage
if not exist "%RSCRIPT%" goto no_rscript
if not exist "%ZIP%" goto no_zip

set "JOB=historical.r"
echo.
echo === historical.r ===
"%RSCRIPT%" "%HERE%historical.r" "%ZIP%"
if errorlevel 1 goto failed

set "JOB=limit_up_down.r"
echo.
echo === limit_up_down.r ===
if defined ENVS "%RSCRIPT%" "%HERE%limit_up_down.r" "%ZIP%" "%ENVS%"
if not defined ENVS "%RSCRIPT%" "%HERE%limit_up_down.r" "%ZIP%"
if errorlevel 1 goto failed

set "JOB=trading_data.r"
echo.
echo === trading_data.r ===
"%RSCRIPT%" "%HERE%trading_data.r" "%ZIP%"
if errorlevel 1 goto failed

echo.
echo ok  all three jobs done; the log is in LOG_DIR
pause
exit /b 0

:usage
echo usage: run_phase1.cmd C:\path\to\phase1-YYYYMMDD.zip ["Test|Pilot|Prod"]
goto stop

:no_rscript
echo XX  Rscript not found: "%RSCRIPT%"
echo     set RSCRIPT at the top of %~nx0
goto stop

:no_zip
echo XX  no such zip: "%ZIP%"
goto stop

:failed
echo.
echo XX  %JOB% failed, exit code %ERRORLEVEL%; the jobs after it were not run.
echo     See the XX lines above, and LOG_DIR\phase1-YYYYMMDD.log.

:stop
pause
exit /b 1
