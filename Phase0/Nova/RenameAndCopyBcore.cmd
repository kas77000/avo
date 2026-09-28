rem Rename every AbaqueNova*.csv to AbaqueNova*_bcore.csv, then copy the
rem _bcore files to two places. A name already ending in _bcore is left alone,
rem and robocopy only copies files that are new or changed.

cd /d "C:\path\to\files"

for /f "delims=" %%F in ('dir /b "AbaqueNova*.csv" ^| findstr /v /i /l /e /c:"_bcore.csv"') do ren "%%F" "%%~nF_bcore.csv"

robocopy "C:\path\to\files" "C:\path\to\destination1" "AbaqueNova*_bcore.csv"
robocopy "C:\path\to\files" "C:\path\to\destination2" "AbaqueNova*_bcore.csv"

pause
