rem Put everything in the folder below on the clipboard, as if you selected it
rem all in Explorer and pressed Ctrl+C. Then run PasteFromClipboard.cmd on the
rem other side (laptop or Citrix session) to drop it there. Works both ways:
rem keep a copy of both scripts on each machine.

set "FOLDER=C:\path\to\outbox"

powershell -NoProfile -STA -Command "Set-Clipboard -Path (Join-Path $env:FOLDER '*')"

pause
