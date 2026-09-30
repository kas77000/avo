@echo off
rem run_phase1.cmd: the day's zip from AB -> the Historical tick files,
rem limitUpDown.csv and TradingData.csv, one R job after the other.
rem
rem     run_phase1.cmd C:\path\to\phase1-YYYYMMDD.zip
rem     run_phase1.cmd C:\path\to\phase1-YYYYMMDD.zip "Test|Prod"
rem
rem The second argument, quoted, is the environments limitUpDown.csv is
rem copied to; without it, all three. Stops at the first job that fails.
rem
rem Only the jobs the zip was made for run (AB's extract.py --for, in the
rem zip's manifest): historical.r needs ticks, limit_up_down.r luld,
rem trading_data.r td. The others are skipped, and said so; a skipped job
rem is not a failure.
rem
rem Last, whatever the jobs did, mail_report.r writes the recap to LOG_DIR
rem and mails it (when settings.r says where). Its own failure changes
rem nothing: the last line and the exit code are the jobs'.
setlocal

rem R 3.2.2's Rscript on this machine.
set "RSCRIPT=C:\path\to\R-3.2.2\bin\x64\Rscript.exe"

set "HERE=%~dp0"
set "ZIP=%~1"
set "ENVS=%~2"
set "SKIPPED="
set "RAN="
set "FAILED="

rem No ( ) blocks below: a path with a ) in it would end one early.
if "%ZIP%"=="" goto usage
if not exist "%RSCRIPT%" goto no_rscript
if not exist "%ZIP%" goto no_zip

set "JOB=historical.r"
set "USE=ticks"
call :has
if errorlevel 3 goto skip_historical
if errorlevel 1 goto no_manifest
echo.
echo === historical.r ===
"%RSCRIPT%" "%HERE%historical.r" "%ZIP%"
if errorlevel 1 goto failed
set "RAN=%RAN% %JOB%"
goto after_historical
:skip_historical
call :skip
:after_historical

set "JOB=limit_up_down.r"
set "USE=luld"
call :has
if errorlevel 3 goto skip_limit_up_down
if errorlevel 1 goto no_manifest
echo.
echo === limit_up_down.r ===
if defined ENVS "%RSCRIPT%" "%HERE%limit_up_down.r" "%ZIP%" "%ENVS%"
if not defined ENVS "%RSCRIPT%" "%HERE%limit_up_down.r" "%ZIP%"
if errorlevel 1 goto failed
set "RAN=%RAN% %JOB%"
goto after_limit_up_down
:skip_limit_up_down
call :skip
:after_limit_up_down

set "JOB=trading_data.r"
set "USE=td"
call :has
if errorlevel 3 goto skip_trading_data
if errorlevel 1 goto no_manifest
echo.
echo === trading_data.r ===
"%RSCRIPT%" "%HERE%trading_data.r" "%ZIP%"
if errorlevel 1 goto failed
set "RAN=%RAN% %JOB%"
goto after_trading_data
:skip_trading_data
call :skip
:after_trading_data

set "STATUS=ok"
call :mail
echo.
if defined SKIPPED goto some_done
echo ok  all three jobs done; the log is in LOG_DIR
pause
exit /b 0

:some_done
echo ok  jobs done; skipped, not in this zip:%SKIPPED%; the log is in LOG_DIR
pause
exit /b 0

rem Exit 0 when the zip is made for %USE%, 3 when it is not, 1 when its
rem manifest cannot be read.
:has
"%RSCRIPT%" "%HERE%common.r" --zip-has %USE% "%ZIP%"
exit /b %ERRORLEVEL%

:skip
echo.
echo ..  %JOB% skipped: this zip was not made for %USE%
set "SKIPPED=%SKIPPED% %JOB%"
exit /b 0

rem The recap: what ran, what was skipped, what failed. Always exit 0.
:mail
echo.
echo === mail_report.r ===
"%RSCRIPT%" "%HERE%mail_report.r" "%ZIP%" "--status=%STATUS%" "--ran=%RAN%" "--skipped=%SKIPPED%" "--failed=%FAILED%"
if errorlevel 1 echo !!  mail_report.r failed; no recap. The jobs' result is below.
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

:no_manifest
echo XX  could not read the manifest of "%ZIP%"; no job was run after it.
goto stop

:failed
set "CODE=%ERRORLEVEL%"
set "FAILED=%JOB%"
set "STATUS=failed"
call :mail
echo.
echo XX  %JOB% failed, exit code %CODE%; the jobs after it were not run.
echo     See the XX lines above, and LOG_DIR\phase1-YYYYMMDD.log.

:stop
pause
exit /b 1
