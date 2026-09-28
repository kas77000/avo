rem Paste the files on the clipboard into the folder below, exactly as Ctrl+V
rem in Explorer would: files copied on the other machine come through Citrix
rem the same way, and Windows asks before replacing a file already there.

set "FOLDER=C:\path\to\inbox"

powershell -NoProfile -STA -Command "(New-Object -ComObject Shell.Application).NameSpace($env:FOLDER).Self.InvokeVerb('paste')"

pause
