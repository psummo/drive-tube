#!/data/data/com.termux/files/usr/bin/bash
# Ferma drive-tube su Android (Termux) e lascia dormire il telefono.
pkill -f "termux/start[.]sh" 2>/dev/null
pkill -f "python.* yt_mjpeg[.]py" 2>/dev/null
command -v termux-wake-unlock >/dev/null && termux-wake-unlock
echo "drive-tube fermato."
