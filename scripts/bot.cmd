@echo off
rem Основной бот. Нужны BOT_TOKEN и, в сетях с блокировкой Telegram, BOT_PROXY.
setlocal
cd /d "%~dp0.."
.venv\Scripts\python.exe -m app.bot.main
