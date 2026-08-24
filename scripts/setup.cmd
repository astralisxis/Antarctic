@echo off
rem Первичная установка. Запускать можно откуда угодно — сами переходим в корень,
rem иначе venv и .env создаются рядом со скриптом.
setlocal
cd /d "%~dp0.."

if not exist .venv (
    echo Создаю виртуальное окружение...
    python -m venv .venv || goto :fail
)

echo Ставлю зависимости...
.venv\Scripts\python.exe -m pip install --upgrade pip -q || goto :fail
.venv\Scripts\python.exe -m pip install --upgrade setuptools wheel -q || goto :fail
.venv\Scripts\python.exe -m pip install -r requirements.txt -q || goto :fail

if not exist .env (
    echo Копирую .env.example в .env — заполните токены.
    copy .env.example .env >nul
)

echo Применяю миграции...
.venv\Scripts\python.exe -m alembic upgrade head || goto :fail
.venv\Scripts\python.exe -m app.tools.initdb || goto :fail

echo.
echo Готово. Запуск админки: scripts\admin.cmd
exit /b 0

:fail
echo.
echo Установка прервана, код %errorlevel%.
exit /b %errorlevel%
