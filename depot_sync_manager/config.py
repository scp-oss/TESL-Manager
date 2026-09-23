# ==================== config.py ====================
import os
from pathlib import Path


def get_manager_commit() -> str:
    """Короткий git-хэш текущего чекаута — та же идея и реализация, что
    TESL/launcher/config.py::get_launcher_commit() (прямой запрос
    пользователя показать коммит в заголовке окна и здесь тоже, тем же
    способом, что уже был сделан для лаунчера, см. его CLAUDE.md
    "Window title format: TESL [commit version]"). Работает только при
    запуске из git-чекаута — git сам находит .git, поднимаясь от cwd
    вверх по дереву. В собранном PyInstaller .exe .git не бандлится —
    возвращает "?", это не ошибка, просто неоткуда взять."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "?"


MANAGER_VERSION = "1.0.0"


def get_window_title() -> str:
    """Заголовок главного окна — "TESL-Manager [<версия>] [<git-хэш>]" —
    прямой запрос пользователя (2026-09-23, "вместо Uploder пусть будет
    TESL-Manager [vers] [commit]", повторено явно тем же текстом ещё раз —
    именно ДВЕ отдельные квадратные скобки, версия первой, в таком
    порядке, а не единая скобка "[commit version]", как у TESL (лаунчер,
    см. его config.py::get_window_title()) — формат здесь сознательно
    другой, несмотря на общий принцип "коммит в заголовке", раз
    пользователь явно попросил именно так. Вычисляется при каждом вызове
    (main_window.py::__init__), не константа времени импорта — ради
    живого git-хэша, если сессия перезапустит окно после git pull без
    перезапуска процесса целиком."""
    return f"TESL-Manager [{MANAGER_VERSION}] [{get_manager_commit()}]"


def calculate_max_workers():
    cores = os.cpu_count() or 4
    if cores <= 4:
        return max(4, cores * 2)
    elif cores <= 8:
        return max(8, cores * 2)
    else:
        return min(32, cores * 2)


def calculate_batch_size():
    try:
        memory_gb = (
            os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024.0 ** 3)
            if hasattr(os, "sysconf") else 8
        )
    except Exception:
        memory_gb = 8
    if memory_gb < 4:   return 100
    if memory_gb < 8:   return 250
    if memory_gb < 16:  return 500
    return 1000


MAX_WORKERS = calculate_max_workers()
BATCH_SIZE  = calculate_batch_size()
CHUNK_SIZE  = 1024 * 1024
MAX_RETRIES = 3
TIMEOUT     = (30, 300)

APPDATA_DIR = Path(os.getenv("APPDATA") or Path.home()) / "Uploder"
APPDATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_FILE   = APPDATA_DIR / "config.json"
MANIFEST_FILE = APPDATA_DIR / "manifest.json"
LOG_FILE      = APPDATA_DIR / "uploader.log"
DB_FILE       = APPDATA_DIR / "uploder.db"   # локальный операторский индекс релизов/чанков, см. db.py

DEFAULT_EXCLUDES = [
    ".git", ".svn", ".hg", ".vs", ".idea", "__pycache__",
    "*.tmp", "*.temp", "*.log", "*.bak", "*.backup",
    "thumbs.db", "desktop.ini", ".DS_Store",
]

DEFAULT_WEBDAV_CONFIG = {
    "server_url":  "",
    "username":    "",
    "password":    "",
    "remote_path": "/",
    "verify_ssl":  True,
}

DEFAULT_DEPOT_CONFIG = {
    "app_id":     "my_app",
    "depot_id":   1001,
    "channel":    "stable",
    "chunk_size": 4 * 1024 * 1024,
}

# Три компонента сборки. Хранятся в config.json под ключом "components".
# Каждый компонент:
#   local_dir  — локальный путь к папке (пустая строка = не задан)
#   included   — включён ли компонент в текущий релиз
#   excludes   — список исключений специфичных для этого компонента
COMPONENT_NAMES = ["Skyrim", "MO2p", "MO2ext"]

DEFAULT_COMPONENTS_CONFIG = {
    "Skyrim": {
        "local_dir": "",
        "included":  True,
        "excludes":  [],
    },
    "MO2p": {
        "local_dir": "",
        "included":  True,
        "excludes":  [],
    },
    "MO2ext": {
        "local_dir": "",
        "included":  True,
        "excludes":  [],
    },
}
