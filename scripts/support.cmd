@echo off
rem Бот поддержки. Нужны SUPPORT_BOT_TOKEN и, в сетях с блокировкой Telegram, BOT_PROXY.
rem Он же разносит клиентам ответы из админки.
setlocal
cd /d "%~dp0.."
.venv\Scripts\python.exe -m app.bot.support_main
