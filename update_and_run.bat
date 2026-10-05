@echo off
cd /d "%~dp0"

echo === TESL-MANAGER: update from git + launch ===
echo.

rem Hard-sync to origin/main instead of `git pull` - guarantees this is
rem EXACTLY what's on GitHub, no ambiguity from fast-forward/errorlevel
rem quirks. This discards any local changes to tracked files (not
rem untracked ones like depot_sync_manager/secrets_local.py or panel.env-
rem style configs) - fine for a checkout that's only ever meant to run
rem the app, not to develop on. Same convention as TESL's own
rem update_and_run.bat - keep both in sync if this one ever changes.
git fetch origin main
git reset --hard origin/main

echo.
echo Now running commit:
git log -1 --oneline
echo.

cd depot_sync_manager

rem WebDAV password / panel setup code: env var / secrets_local.py /
rem cached APPDATA file are all checked automatically by the app itself.
rem If none of them has it, it shows its own dialog on first run and
rem saves it - nothing to do here.

rem Launch parameters go here if ever needed, e.g.:
rem set TESL_MANAGER_SOME_FLAG=1
rem Any args passed to this .bat are forwarded to main.py as-is.

echo.
echo Starting TESL-Manager...
echo.
python main.py %*

echo.
pause
