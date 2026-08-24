@echo off
rem Public site and Telegram Mini App listen only on localhost.
setlocal
cd /d "%~dp0.."
.venv\Scripts\python.exe -m uvicorn app.web.main:app --host 127.0.0.1 --port 8081
