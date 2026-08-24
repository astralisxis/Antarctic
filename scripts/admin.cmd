@echo off
rem Admin listens only on localhost; scripts\tunnel.cmd publishes it.
setlocal
cd /d "%~dp0.."
.venv\Scripts\python.exe -m uvicorn app.admin.main:app --host 127.0.0.1 --port 8080
