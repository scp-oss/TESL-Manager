@echo off
chcp 65001 >nul
cd /d %~dp0

echo === TESL-Panel: публикация с упаковкой чанков (без сборки .exe) ===
echo.

set /p LOCAL_DIR="Папка для публикации (полный путь): "
if "%LOCAL_DIR%"=="" (
    echo Папка не указана, выход.
    pause
    exit /b 1
)

set /p PANEL_URL="URL панели [https://tesl-panel.neth.de5.net]: "
if "%PANEL_URL%"=="" set PANEL_URL=https://tesl-panel.neth.de5.net

set /p PROJECT="Имя проекта [TESVAE]: "
if "%PROJECT%"=="" set PROJECT=TESVAE

set /p TOKEN="Upload-токен: "
if "%TOKEN%"=="" (
    echo Токен не указан, выход.
    pause
    exit /b 1
)

echo.
echo Проверяем зависимости (requests, PyQt6)...
python -m pip install -q -r requirements.txt

echo.
echo Запускаем публикацию...
echo.
python publish_packed.py --local-dir "%LOCAL_DIR%" --panel-url "%PANEL_URL%" --project "%PROJECT%" --token "%TOKEN%"

echo.
pause
