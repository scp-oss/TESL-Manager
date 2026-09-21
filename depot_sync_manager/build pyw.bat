@echo off
REM Переходим в папку, где находится батник
cd /d %~dp0
REM Запускаем Python-скрипт
pyinstaller --noconsole --onefile --windowed --icon=icon.ico --add-data "icon.ico;." main.py
pause
