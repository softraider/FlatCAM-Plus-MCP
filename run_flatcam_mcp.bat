@echo off
setlocal
cd /d %~dp0
rem Starts FlatCAM Plus with the FlatCAM Plus MCP bridge enabled (local only, token protected).
set FLATCAM_MCP_BRIDGE=1
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" flatcam.py %*
) else (
    start "" pythonw flatcam.py %*
)
