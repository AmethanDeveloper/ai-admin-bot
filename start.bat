@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Бот записи на пробный урок

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python не найден. Установите его с https://www.python.org/downloads/ ^(галочка "Add python.exe to PATH"^) и запустите этот файл снова.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Первый запуск: создаю окружение, это займёт минуту...
  %PY% -m venv .venv || (echo Не удалось создать окружение. & pause & exit /b 1)
)

".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || (echo Не удалось установить библиотеки. Проверьте интернет. & pause & exit /b 1)

echo.
echo Бот запущен. Не закрывайте это окно, пока бот нужен. Остановить: Ctrl+C.
echo.
".venv\Scripts\python.exe" bot.py
pause
