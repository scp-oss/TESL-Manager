# ==================== depot_sync_manager.py ====================
"""
DepotSyncManager — сетевой слой depot-системы.
Откалиброван под реальные параметры nethunter.sytes.net:

  Probe results:
    ✅ PROPFIND Depth: infinity  — работает, 0.04s
    ✅ PUT 4 MB  @ 10.1 MB/s    — стандартный PUT достаточен
    ✅ PUT 32 MB @ 79.0 MB/s    — локалка, timeout можно держать умеренным
    ✅ Range GET (206)           — resume поддерживается
    ✅ MKCOL                    — 201 (не 405), т.е. сервер не возвращает 405
    ✅ DELETE рекурсивный       — работает на директории
    ❌ NC Chunked MOVE          — не нужен (чанки 4MB через PUT отлично)
"""

import os
import json
import shutil
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple
from datetime import datetime

import requests
from requests.auth import HTTPBasicAuth
from PyQt6.QtCore import QObject, QThread, pyqtSignal

from chunk_manager import (
    ChunkManager, DepotManifest, DepotDelta, FileEntry, ChunkInfo,
    CHUNKS_DIR, VERSIONS_DIR, DEPOT_META, DEFAULT_CHUNK_SIZE,
)
from pack_writer import (
    PackWriter, DEFAULT_PACK_SIZE, HDD_PACK_SIZE,
    write_chunk_index_db, read_chunk_index_db,
)
from threads import ThreadSafeWorker

PACKS_DIR = "packs"
CHUNK_INDEX_NAME = "chunk_index.db"


# ── Константы (откалиброваны по probe) ───────────────────────────────────────

TIMEOUT_CONNECT  = 15
# 300с, не 120 — см. тот же живой инцидент 2026-09-24 в panel_client.py
# (согласовано с TESL-Panel's nginx-таймаутами) — крупный pack-файл на
# медленном канале может не уложиться в старые 120с даже без ошибки
# сервера.
TIMEOUT_PUT      = 300
TIMEOUT_GET      = 60
TIMEOUT_PROPFIND = 30
TIMEOUT_META     = 20

MAX_RETRIES      = 3
RETRY_BACKOFF    = (1, 3, 7)

UPLOAD_WORKERS   = 4

# Заливка pack-файлов на SSD-сервер — параллельно, тем же принципом, что
# UPLOAD_WORKERS у per-chunk протокола (SSD не деградирует на случайном
# доступе, узкое место — сеть/CPU, не диск). Для HDD параллелизм НЕ
# используется вообще (строго один PUT за раз) — см. execute_sync_packed()
# и CLAUDE.md "Алгоритм заливки под SSD/HDD" за обоснование.
UPLOAD_WORKERS_PACKS_SSD = 4

DAV_NS = "{DAV:}"


# Живой инцидент (2026-09-24): пауза сервиса на сервере ровно в момент
# заливки (`sudo systemctl stop tesl-panel.service` во время деплоя,
# сделанного одновременно с текущей публикацией) дала честный, но
# фатальный для клиента 502 от nginx — nginx сам жив, апстрима за ним
# нет — вся публикация оборвалась на pack-00015.bin, пользователю
# оставалось начинать заново. Встроенный NextcloudDAV/PanelHTTP._retry()
# лечит только СЫРЫЕ сетевые исключения requests (обрыв соединения/
# таймаут), коротким окном (~11с суммарно, RETRY_BACKOFF) — реальный
# HTTP-ответ 502/503/504 (сервис отвечает, апстрима нет/перегружен)
# requests вообще не считает исключением, значит там не ретраится
# вовсе. is_transient_last_error()/DepotSyncManager._retry_until_recovered()
# ниже — дополнительный, куда более терпеливый слой ПОВЕРХ put()/
# put_file(): если конкретная попытка похожа на "сервер временно
# недоступен" — публикация не проваливается, а ждёт с бэкоффом и
# повторяет САМ ЭТОТ файл (не всю публикацию с начала), пока не
# восстановится или пользователь не нажмёт "Стоп". Всё остальное
# (401/403/400/404/413 и т.п. — настоящая, не временная проблема)
# по-прежнему проваливает публикацию сразу же, как и раньше — бесконечно
# ждать восстановления там смысла нет.
_TRANSIENT_STATUS_CODES = {502, 503, 504}
_TRANSIENT_EXC_PREFIXES = ("ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout")


def _is_transient_last_error(last_error: str) -> bool:
    if not last_error:
        return False
    if last_error.startswith("HTTP "):
        try:
            code = int(last_error.split(":", 1)[0].split()[1])
        except (IndexError, ValueError):
            return False
        return code in _TRANSIENT_STATUS_CODES
    return last_error.startswith(_TRANSIENT_EXC_PREFIXES)


def _fmt_eta(seconds: float) -> str:
    """Тот же формат, что у TESL (лаунчер, core/chunk_installer.py::
    _fmt_eta) — прямой запрос пользователя "добавь прогресс бар с данными
    как в лаунчере" (2026-09-23): статус-бар публикации должен выглядеть
    так же, как статус-бар установки, а не изобретать свой формат."""
    if seconds < 0 or seconds != seconds:   # NaN check без импорта math
        return "?"
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}ч{m:02d}м"
    if m:
        return f"{m}м{s:02d}с"
    return f"{s}с"

NC_PROPFIND_BODY = b"""<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:getcontentlength/>
    <D:getlastmodified/>
    <D:resourcetype/>
  </D:prop>
</D:propfind>"""

NC_PROPFIND_HEADERS = {
    "Content-Type": "application/xml; charset=utf-8",
    "Accept":       "application/xml",
}


# ── NextcloudDAV ──────────────────────────────────────────────────────────────

class NextcloudDAV:
    """
    Низкоуровневый WebDAV-клиент для Nextcloud.
    
    Особенности после probe:
      • PROPFIND Depth:infinity работает → используем для листинга chunks/
      • href в PROPFIND-ответе — абсолютный URI-путь (без scheme+host)
      • MKCOL возвращает 201 (не 405) на существующие папки → проверяем exists() первым
      • DELETE работает рекурсивно на директории
      • Стандартный PUT до 32+ MB без проблем
    """

    def __init__(
        self,
        server_url: str,
        username:   str,
        password:   str,
        verify_ssl: bool = True,
    ):
        self.base = server_url.rstrip("/")
        from urllib.parse import urlparse
        parsed = urlparse(self.base)
        self._dav_prefix = parsed.path

        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(username, password)
        self.session.verify = verify_ssl
        self.session.headers.update({
            "User-Agent": "Uploder-Depot/2.0",
        })
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=8,
            pool_maxsize=12,
            max_retries=0,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://",  adapter)

        # Для хранения последнего ответа (для отладки)
        self.last_response = None
        # last_error — см. panel_client.py::PanelHTTP.last_error (тот же
        # живой случай 2026-09-24: 413 от nginx на реальном сервере
        # маскировался общим "Не удалось загрузить" — здесь тот же фикс,
        # для WebDAV-транспорта).
        self.last_error = ""

    # ── URL с правильным кодированием спецсимволов ───────────────────────────

    def url(self, *parts: str) -> str:
        """
        Собирает URL с кодированием спецсимволов: # → %23, пробелы → %20 и т.д.
        Каждый сегмент пути кодируется отдельно, слэши остаются разделителями.
        """
        segs = []
        for p in parts:
            if not p:
                continue
            # Убираем ведущие и замыкающие слеши, разбиваем на сегменты
            for segment in p.strip("/").split("/"):
                if segment:
                    # Кодируем спецсимволы, оставляя безопасные (буквы, цифры, -_.~)
                    encoded = urllib.parse.quote(segment, safe="")
                    segs.append(encoded)
        path = "/".join(segs)
        return self.base + ("/" + path if path else "")

    # ── Retry wrapper ─────────────────────────────────────────────────────────

    def _retry(self, fn, *args, **kwargs):
        last_exc = None
        for attempt, delay in enumerate(RETRY_BACKOFF[:MAX_RETRIES]):
            try:
                return fn(*args, **kwargs)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last_exc = e
                if attempt < MAX_RETRIES - 1:
                    time.sleep(delay)
        raise last_exc

    # ── PROPFIND ──────────────────────────────────────────────────────────────

    def propfind(self, path: str, depth: str = "0") -> Optional[ET.Element]:
        try:
            r = self._retry(
                self.session.request,
                "PROPFIND",
                self.url(path),
                headers={**NC_PROPFIND_HEADERS, "Depth": depth},
                data=NC_PROPFIND_BODY,
                timeout=TIMEOUT_PROPFIND,
            )
            self.last_response = r
            if r.status_code in (200, 207):
                return ET.fromstring(r.content)
            return None
        except Exception:
            return None

    def exists(self, path: str) -> bool:
        return self.propfind(path, depth="0") is not None

    # ── PROPFIND href парсер ──────────────────────────────────────────────────

    def _href_to_rel(self, href: str, base_path: str) -> Optional[str]:
        decoded = urllib.parse.unquote(href).rstrip("/")
        full_base = (self._dav_prefix.rstrip("/") + "/" + base_path.strip("/"))
        if decoded == full_base:
            return None
        if decoded.startswith(full_base + "/"):
            return decoded[len(full_base) + 1:]
        return None

    def _is_collection(self, response_el: ET.Element) -> bool:
        for propstat in response_el.findall(f"{DAV_NS}propstat"):
            rt = propstat.find(f".//{DAV_NS}resourcetype/{DAV_NS}collection")
            if rt is not None:
                return True
        return False

    # ── Листинг файлов ────────────────────────────────────────────────────────

    def list_files_infinity(self, path: str) -> Set[str]:
        result: Set[str] = set()
        try:
            r = self._retry(
                self.session.request,
                "PROPFIND",
                self.url(path),
                headers={**NC_PROPFIND_HEADERS, "Depth": "infinity"},
                data=NC_PROPFIND_BODY,
                timeout=TIMEOUT_PROPFIND,
            )
            self.last_response = r
            if r.status_code not in (200, 207):
                return result

            root = ET.fromstring(r.content)
            for resp in root.findall(f"{DAV_NS}response"):
                if self._is_collection(resp):
                    continue
                href_el = resp.find(f"{DAV_NS}href")
                if href_el is None or not href_el.text:
                    continue
                rel = self._href_to_rel(href_el.text, path)
                if rel:
                    result.add(rel)
        except Exception:
            pass
        return result

    def list_chunk_ids(self, chunks_path: str) -> Set[str]:
        files = self.list_files_infinity(chunks_path)
        ids = set()
        for f in files:
            parts = f.split("/")
            if len(parts) == 2:
                ids.add(parts[1])
            elif len(parts) == 1:
                ids.add(parts[0])
        return ids

    # ── MKCOL ─────────────────────────────────────────────────────────────────

    def mkcol(self, path: str) -> bool:
        parts = path.strip("/").split("/")
        current = ""
        for part in parts:
            current = (current + "/" + part).lstrip("/")
            if self.exists(current):
                continue
            try:
                r = self._retry(
                    self.session.request,
                    "MKCOL",
                    self.url(current),
                    timeout=TIMEOUT_CONNECT,
                )
                self.last_response = r
                if r.status_code not in (200, 201, 204, 405):
                    return False
            except Exception:
                return False
        return True

    # ── PUT ───────────────────────────────────────────────────────────────────

    def put(
        self,
        path:         str,
        data:         bytes,
        content_type: str = "application/octet-stream",
    ) -> bool:
        self.last_error = ""
        try:
            r = self._retry(
                self.session.put,
                self.url(path),
                data=data,
                headers={"Content-Type": content_type},
                timeout=TIMEOUT_PUT,
            )
            self.last_response = r
            if r.status_code in (200, 201, 204):
                return True
            self.last_error = f"HTTP {r.status_code}: {r.text[:300]}"
            return False
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return False

    def put_file(
        self,
        path:         str,
        local_path:   Path,
        content_type: str = "application/octet-stream",
    ) -> bool:
        self.last_error = ""
        try:
            with open(local_path, "rb") as f:
                r = self._retry(
                    self.session.put,
                    self.url(path),
                    data=f,
                    headers={"Content-Type": content_type},
                    timeout=TIMEOUT_PUT,
                )
            self.last_response = r
            if r.status_code in (200, 201, 204):
                return True
            self.last_error = f"HTTP {r.status_code}: {r.text[:300]}"
            return False
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return False

    # ── GET ───────────────────────────────────────────────────────────────────

    def get_bytes(self, path: str) -> Optional[bytes]:
        try:
            r = self._retry(
                self.session.get,
                self.url(path),
                timeout=TIMEOUT_META,
            )
            self.last_response = r
            return r.content if r.status_code == 200 else None
        except Exception:
            return None

    def get_range(self, path: str, start: int, end: int) -> Optional[bytes]:
        try:
            r = self._retry(
                self.session.get,
                self.url(path),
                headers={"Range": f"bytes={start}-{end}"},
                timeout=TIMEOUT_GET,
            )
            self.last_response = r
            return r.content if r.status_code == 206 else None
        except Exception:
            return None

    # ── DELETE ────────────────────────────────────────────────────────────────

    def delete(self, path: str) -> bool:
        try:
            r = self._retry(
                self.session.delete,
                self.url(path),
                timeout=TIMEOUT_CONNECT,
            )
            self.last_response = r
            return r.status_code in (200, 204, 404)
        except Exception:
            return False

    # ── Connection test ───────────────────────────────────────────────────────

    def test_connection(self) -> Tuple[bool, str]:
        try:
            r = self.session.request(
                "PROPFIND",
                self.base + "/",
                headers={**NC_PROPFIND_HEADERS, "Depth": "0"},
                data=NC_PROPFIND_BODY,
                timeout=TIMEOUT_CONNECT,
            )
            self.last_response = r
            codes = {
                207: (True,  "Подключено (Nextcloud WebDAV)"),
                200: (True,  "Подключено"),
                401: (False, "Ошибка авторизации — проверьте логин/пароль"),
                403: (False, "Доступ запрещён (403)"),
                404: (False, f"Путь не найден: {self.base}"),
            }
            return codes.get(r.status_code, (False, f"HTTP {r.status_code}"))
        except requests.exceptions.SSLError as e:
            return False, f"SSL ошибка: {e}"
        except requests.exceptions.ConnectionError as e:
            return False, f"Нет соединения: {e}"
        except requests.exceptions.Timeout:
            return False, f"Таймаут подключения ({TIMEOUT_CONNECT}s)"
        except Exception as e:
            return False, str(e)

    def close(self):
        self.session.close()


# ── DepotSyncManager ──────────────────────────────────────────────────────────

class DepotSyncManager(QObject):
    """
    Высокоуровневый менеджер синхронизации депо.
    
    Workflow (publisher):
      1. fetch_remote_manifest()    → текущий манифест с сервера (или None)
      2. ChunkManager.scan_directory() → новый DepotManifest
      3. ChunkManager.compute_delta()  → DepotDelta
      4. [UI confirm]
      5. execute_sync()             → параллельная загрузка чанков + манифест
    """

    log      = pyqtSignal(str)
    progress = pyqtSignal(int, int, str)

    def __init__(self, config: dict):
        super().__init__()
        self.config      = config
        self.webdav_cfg  = config.get("webdav", {})
        self.panel_cfg   = config.get("panel", {})
        # "webdav" (по умолчанию, без изменений) | "panel" — публикация
        # напрямую в TESL-Panel вместо Nextcloud, см. panel_client.py и
        # CLAUDE.md "Публикация напрямую в наш сервис". Ничего ниже в этом
        # классе не знает, какой бэкенд активен — оба транспорта реализуют
        # один и тот же интерфейс (exists/mkcol/put/put_file/get_bytes/
        # list_chunk_ids/test_connection/close).
        self.backend     = config.get("backend", "webdav")
        self.remote_path = self.webdav_cfg.get("remote_path", "").strip("/")
        # Упаковка чанков в pack-файлы вместо chunks/<xx>/<id> — см.
        # execute_sync_packed()/pack_writer.py и CLAUDE.md "Упаковка чанков
        # в pack-файлы". Отдельная от backend ось — можно паковать чанки И
        # публиковать через WebDAV, или не паковать и публиковать через
        # panel, любая комбинация валидна.
        self.use_packs = bool(config.get("use_packs", False))
        # Тип диска на сервере — единственная ручная настройка, всё
        # остальное (размер pack-файла, параллелизм заливки) выводится из
        # неё автоматически (прямой запрос 2026-09-23: "настройки депо
        # должны определяться автоматически" + отдельный запрос "проработай
        # тщательно алгоритм для hdd"). "hdd" — дефолт по умолчанию:
        # безопаснее ошибиться в сторону последовательной заливки на SSD
        # (просто чуть медленнее, чем могло бы быть), чем ошибиться в
        # сторону параллельной заливки на HDD (реальная деградация из-за
        # random I/O, ровно то, из-за чего вообще появился pack_writer.py —
        # см. "Упаковка чанков в pack-файлы" выше, iostat-подтверждение
        # случайного чтения на РЕАЛЬНОМ сервере пользователя).
        self.disk_mode = config.get("depot", {}).get("disk_mode", "hdd")
        _auto_pack_size = DEFAULT_PACK_SIZE if self.disk_mode == "ssd" else HDD_PACK_SIZE
        self.pack_size = int(config.get("depot", {}).get("pack_size") or _auto_pack_size)
        self._dav = None   # NextcloudDAV | PanelHTTP, в зависимости от self.backend

    def _get_dav(self):
        if self._dav is None:
            if self.backend == "panel":
                from panel_client import PanelHTTP
                cfg = self.panel_cfg
                self._dav = PanelHTTP(
                    base_url   = cfg.get("base_url", ""),
                    build_id   = cfg.get("build_id", ""),
                    token      = cfg.get("token", ""),
                    verify_ssl = cfg.get("verify_ssl", True),
                )
            else:
                cfg = self.webdav_cfg
                self._dav = NextcloudDAV(
                    server_url = cfg.get("server_url", ""),
                    username   = cfg.get("username", ""),
                    password   = cfg.get("password", ""),
                    verify_ssl = cfg.get("verify_ssl", True),
                )
        return self._dav

    def test_connection(self) -> Tuple[bool, str]:
        return self._get_dav().test_connection()

    # ── Paths ─────────────────────────────────────────────────────────────────

    def _rp(self, *parts: str) -> str:
        segs = [self.remote_path] + [p.strip("/") for p in parts if p]
        return "/".join(s for s in segs if s)

    # ── Remote state ──────────────────────────────────────────────────────────

    def ensure_depot_structure(self) -> bool:
        dav = self._get_dav()
        dirs = [self.remote_path, self._rp(VERSIONS_DIR)]
        # packs/ вместо chunks/ при упаковке — оба варианта безвредно
        # создать заранее, даже если этот конкретный запуск использует
        # только один из них (mkcol на panel-транспорте и так no-op, см.
        # panel_client.py).
        dirs.append(self._rp(PACKS_DIR) if self.use_packs else self._rp(CHUNKS_DIR))
        for d in dirs:
            if not d:
                continue
            if not dav.exists(d):
                self.log.emit(f"📁 Создаём: {d}")
                if not dav.mkcol(d):
                    self.log.emit(f"❌ Не удалось создать: {d}")
                    return False
        return True

    def fetch_remote_manifest(self) -> Optional[DepotManifest]:
        data = self._get_dav().get_bytes(self._rp("depot_manifest.json"))
        if data is None:
            return None
        try:
            return DepotManifest.from_json(data.decode("utf-8"))
        except Exception as e:
            self.log.emit(f"⚠️ Ошибка парсинга манифеста: {e}")
            return None

    def fetch_remote_chunk_ids(self, prev_manifest: Optional[DepotManifest]) -> Set[str]:
        if prev_manifest is not None:
            ids = prev_manifest.get_all_chunk_ids()
            self.log.emit(f"📋 Известных чанков (из манифеста): {len(ids)}")
            return ids

        self.log.emit("📡 Сканируем chunks/ на сервере...")
        ids = self._get_dav().list_chunk_ids(self._rp(CHUNKS_DIR))
        self.log.emit(f"📋 Найдено чанков на сервере: {len(ids)}")
        return ids

    # ── Upload chunk with subdir auto-create ──────────────────────────────────

    def _ensure_chunk_subdir(self, chunk_id: str, dav: NextcloudDAV):
        if not hasattr(self, "_created_subdirs"):
            self._created_subdirs: Set[str] = set()
        subdir = self._rp(CHUNKS_DIR, chunk_id[:2])
        if subdir not in self._created_subdirs:
            if not dav.exists(subdir):
                dav.mkcol(subdir)
            self._created_subdirs.add(subdir)

    def _upload_one_chunk(
        self,
        chunk_id:  str,
        data:      bytes,
        dav:       NextcloudDAV,
    ) -> Tuple[str, bool]:
        self._ensure_chunk_subdir(chunk_id, dav)
        path = self._rp(CHUNKS_DIR, chunk_id[:2], chunk_id)
        ok   = dav.put(path, data)
        return chunk_id, ok

    # ── Разрешение локального пути чанка: одна папка (str, старый CLI-путь,
    #    см. publish_packed.py) ИЛИ несколько компонентных папок (dict
    #    {component: local_dir}, см. DepotTab/chunk_manager.scan_components) —
    #    во втором случае rel_path всегда имеет вид "<Компонент>/<путь>".

    @staticmethod
    def _resolve_source_path(local_dir, rel_path: str) -> Path:
        if isinstance(local_dir, dict):
            comp, _, sub_rel = rel_path.partition("/")
            return Path(local_dir.get(comp, "")) / sub_rel
        return Path(local_dir) / rel_path

    # ── Пауза + повтор при временной недоступности сервера (2026-09-24) ────────
    # См. докстринг _is_transient_last_error() выше за полную картину живого
    # инцидента, который это лечит. Используется только в execute_sync_packed()
    # (активный, реально используемый путь публикации — use_packs=True по
    # умолчанию) — легаси per-chunk execute_sync() этим не покрыт, см.
    # CLAUDE.md за явную оговорку, почему это осознанно оставлено вне
    # объёма этого захода.

    def _retry_until_recovered(self, attempt_fn, dav: "NextcloudDAV", stop_fn, label: str) -> bool:
        """attempt_fn() — вызывает РОВНО одну попытку put()/put_file() и
        возвращает её bool-результат; к этому моменту dav.last_error уже
        выставлен (put()/put_file() и так это делают на каждый вызов).
        Не транзиентная ошибка — возвращает False сразу же, без единой
        паузы, как и раньше. Транзиентная — ждёт с растущим (до потолка
        30с) интервалом и повторяет САМ ЭТОТ файл, проверяя stop_fn()
        каждую секунду ожидания (чтобы "Стоп" оставался отзывчивым, а не
        блокировался на весь интервал разом), пока либо не получится,
        либо пользователь не остановит, либо ошибка не окажется уже НЕ
        транзиентной (например, токен успели сменить, пока ждали)."""
        attempt = 0
        while True:
            ok = attempt_fn()
            if ok:
                if attempt > 0:
                    self.log.emit(f"▶ Соединение восстановлено, продолжаем ({label})")
                return True
            if not _is_transient_last_error(dav.last_error):
                return False
            attempt += 1
            wait_s = min(5 * attempt, 30)
            if attempt == 1:
                self.log.emit(
                    f"⏸ Панель недоступна ({dav.last_error}) — "
                    f"пауза, жду восстановления ({label})…"
                )
            elif attempt % 6 == 0:
                self.log.emit(f"⏸ Всё ещё жду панель ({label}), попытка #{attempt}…")
            waited = 0
            while waited < wait_s:
                if stop_fn and stop_fn():
                    self.log.emit("⏹ Остановлено пользователем во время ожидания восстановления")
                    return False
                time.sleep(1)
                waited += 1

    # ── Execute sync ──────────────────────────────────────────────────────────

    def execute_sync(
        self,
        new_manifest:  DepotManifest,
        delta:         DepotDelta,
        local_dir,     # str (одна папка) ИЛИ dict {component: local_dir}
        stop_fn=None,
        pause_fn=None,
    ) -> Tuple[bool, str]:
        dav   = self._get_dav()
        total = len(delta.chunks_to_upload)
        done  = 0
        failed: List[str] = []

        chunk_source: Dict[str, Tuple[str, int, int]] = {}
        for path, entry in new_manifest.files.items():
            for chunk in entry.chunks:
                if chunk.chunk_id in delta.chunks_to_upload:
                    chunk_source[chunk.chunk_id] = (path, chunk.offset, chunk.size)

        if total > 0:
            self.log.emit(f"📦 Загружаем {total} чанков ({UPLOAD_WORKERS} потоков)...")

            def _read_and_upload(chunk_id: str) -> Tuple[str, bool, int]:
                src = chunk_source.get(chunk_id)
                if src is None:
                    return chunk_id, False, 0
                rel_path, offset, size = src
                try:
                    fp = self._resolve_source_path(local_dir, rel_path)
                    with open(fp, "rb") as f:
                        f.seek(offset)
                        data = f.read(size)
                    _, ok = self._upload_one_chunk(chunk_id, data, dav)
                    return chunk_id, ok, size
                except Exception:
                    return chunk_id, False, 0

            # Прогресс-бар "как в лаунчере" (прямой запрос пользователя,
            # 2026-09-23): процент + скорость + ETA вместо голого "Чанк
            # <хэш>…" — то же скользящее среднее (окно сэмплов не чаще
            # раза в 0.5с, экспоненциальное сглаживание alpha=0.3), что
            # уже использует TESL/core/chunk_installer.py для скачивания.
            total_bytes = sum(sz for (_, _, sz) in chunk_source.values())
            uploaded_bytes = 0
            last_sample_t = time.time()
            last_sample_uploaded = 0
            smoothed_speed = 0.0

            with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
                futures = {
                    pool.submit(_read_and_upload, cid): cid
                    for cid in delta.chunks_to_upload
                }
                for future in as_completed(futures):
                    if stop_fn and stop_fn():
                        pool.shutdown(wait=False, cancel_futures=True)
                        return False, "Остановлено пользователем"
                    if pause_fn:
                        pause_fn()

                    chunk_id, ok, size = future.result()
                    done += 1
                    if ok:
                        uploaded_bytes += size

                    now = time.time()
                    sample_dt = now - last_sample_t
                    if sample_dt >= 0.5:
                        inst = (uploaded_bytes - last_sample_uploaded) / sample_dt
                        alpha = 0.3
                        smoothed_speed = inst if smoothed_speed == 0 else (
                            alpha * inst + (1 - alpha) * smoothed_speed)
                        last_sample_t = now
                        last_sample_uploaded = uploaded_bytes
                    pct = int(uploaded_bytes / total_bytes * 100) if total_bytes else 100
                    bytes_remaining = total_bytes - uploaded_bytes
                    eta = _fmt_eta(bytes_remaining / smoothed_speed) if smoothed_speed > 0 else "?"
                    self.progress.emit(
                        int(uploaded_bytes), int(total_bytes),
                        f"{pct}% — ⬆ {smoothed_speed / 1024 / 1024:.1f} MB/s — осталось: {eta}"
                    )

                    if ok:
                        self.log.emit(
                            f"  ✅ {chunk_id[:16]}  "
                            f"({size // 1024} KB)  [{done}/{total}]"
                        )
                    else:
                        failed.append(chunk_id)
                        self.log.emit(f"  ❌ {chunk_id[:16]}  FAILED [{done}/{total}]")
        else:
            self.log.emit("ℹ️  Новых чанков нет")

        if failed:
            return False, (
                f"Ошибка: {len(failed)}/{total} чанков не загружены. "
                f"Манифест НЕ обновлён — сервер остался в предыдущем состоянии."
            )

        self.log.emit(f"📁 Сохраняем версию {new_manifest.build_id}…")
        version_json = new_manifest.to_json().encode("utf-8")
        dav.put(
            self._rp(VERSIONS_DIR, f"{new_manifest.build_id}.json"),
            version_json,
            content_type="application/json",
        )

        self.log.emit("📝 Обновляем depot.json…")
        depot_meta = self._build_depot_meta(new_manifest)
        dav.put(
            self._rp(DEPOT_META),
            json.dumps(depot_meta, ensure_ascii=False, indent=2).encode("utf-8"),
            content_type="application/json",
        )

        self.log.emit("📤 Публикуем depot_manifest.json…")
        manifest_json = new_manifest.to_json().encode("utf-8")
        ok = dav.put(
            self._rp("depot_manifest.json"),
            manifest_json,
            content_type="application/json",
        )
        if not ok:
            return False, f"Критическая ошибка: не удалось обновить depot_manifest.json: {dav.last_error or 'см. лог'}"

        msg = (
            f"✅ Build #{new_manifest.build_number} опубликован! "
            f"({new_manifest.build_id})  "
            f"Загружено чанков: {done}, "
            f"новых/изменённых файлов: {delta.total_new_files}"
        )
        return True, msg

    # ── Упакованная публикация (pack-файлы + chunk_index.db) ────────────────

    def execute_sync_packed(
        self,
        new_manifest:  DepotManifest,
        delta:         DepotDelta,
        local_dir,     # str (одна папка) ИЛИ dict {component: local_dir}
        stop_fn=None,
        pause_fn=None,
    ) -> Tuple[bool, str]:
        """То же самое, что execute_sync() — тот же delta, та же итоговая
        структура depot.json/versions/<key>.json/depot_manifest.json — но
        физическая загрузка чанков идёт через pack-файлы вместо одного
        файла на чанк (см. pack_writer.py и CLAUDE.md "Упаковка чанков в
        pack-файлы"). Включается через config["use_packs"]=True —
        DepotBuildWorker.run() решает, какой из двух методов вызвать, сам
        этот класс не переключается неявно."""
        dav = self._get_dav()
        total = len(delta.chunks_to_upload)
        pack_dir = Path(tempfile.mkdtemp(prefix="tesl_pack_"))
        try:
            if total > 0:
                self.log.emit("📥 Скачиваем текущий chunk_index.db (если есть)…")
                existing_index: Dict[str, Tuple[str, int, int]] = {}
                idx_bytes = dav.get_bytes(self._rp(CHUNK_INDEX_NAME))
                if idx_bytes is not None:
                    tmp_idx = pack_dir / "existing_chunk_index.db"
                    tmp_idx.write_bytes(idx_bytes)
                    existing_index = read_chunk_index_db(tmp_idx)
                    self.log.emit(f"📋 В существующем индексе: {len(existing_index)} чанков")

                chunk_source: Dict[str, Tuple[str, int, int]] = {}
                for path, entry in new_manifest.files.items():
                    for chunk in entry.chunks:
                        if chunk.chunk_id in delta.chunks_to_upload:
                            chunk_source[chunk.chunk_id] = (path, chunk.offset, chunk.size)

                self.log.emit(f"📦 Упаковываем {total} чанков ({self.pack_size / 1024 / 1024:.1f}MB/pack)…")
                pw = PackWriter(pack_dir / "out", pack_size=self.pack_size)
                done = 0
                # Прогресс с процентом/скоростью/ETA — та же схема
                # сглаживания, что у скана (chunk_manager.py::
                # scan_directory) и заливки ниже (прямой запрос
                # пользователя, 2026-09-24: "ета для хешинга всё ещё
                # нет" — этап упаковки страдал тем же самым).
                total_pack_bytes = sum(
                    chunk_source[cid][2] for cid in delta.chunks_to_upload
                    if cid in chunk_source
                )
                packed_bytes = 0
                last_sample_t = time.time()
                last_sample_bytes = 0
                smoothed_speed = 0.0
                # sorted() — тот же порядок, в котором лаунчер и так
                # запрашивает чанки (см. TESL/core/chunk_installer.py) —
                # упаковка в этом же порядке и есть весь смысл схемы
                # (реальная последовательность на диске сервера, не
                # надежда на ФС).
                for cid in sorted(delta.chunks_to_upload):
                    if stop_fn and stop_fn():
                        return False, "Остановлено пользователем"
                    if pause_fn:
                        pause_fn()
                    src = chunk_source.get(cid)
                    if src is None:
                        continue
                    rel_path, offset, size = src
                    try:
                        fp = self._resolve_source_path(local_dir, rel_path)
                        with open(fp, "rb") as f:
                            f.seek(offset)
                            data = f.read(size)
                        pw.add_chunk(cid, data)
                    except Exception as e:
                        return False, f"Ошибка чтения {rel_path} при упаковке: {e}"
                    done += 1
                    packed_bytes += size

                    now = time.time()
                    sample_dt = now - last_sample_t
                    if sample_dt >= 0.5:
                        inst = (packed_bytes - last_sample_bytes) / sample_dt
                        alpha = 0.3
                        smoothed_speed = inst if smoothed_speed == 0 else (
                            alpha * inst + (1 - alpha) * smoothed_speed)
                        last_sample_t = now
                        last_sample_bytes = packed_bytes
                    pct = int(packed_bytes / total_pack_bytes * 100) if total_pack_bytes else 100
                    bytes_remaining = total_pack_bytes - packed_bytes
                    eta = _fmt_eta(bytes_remaining / smoothed_speed) if smoothed_speed > 0 else "?"
                    self.progress.emit(
                        done, total,
                        f"{pct}% — 📦 {smoothed_speed / 1024 / 1024:.1f} MB/s — "
                        f"осталось: {eta} — {cid[:12]}…"
                    )
                pw.finalize()

                self.log.emit(
                    f"📤 Загружаем {len(pw.pack_paths)} pack-файл(ов) "
                    f"(режим: {self.disk_mode.upper()})…"
                )
                # Прогресс-бар "как в лаунчере" (прямой запрос пользователя,
                # 2026-09-23) — та же схема сглаживания, что в execute_sync()
                # выше, по гранулярности pack-файлов (нет byte-level
                # callback'а внутри dav.put_file()).
                total_upload_bytes = sum(p.stat().st_size for p in pw.pack_paths)
                uploaded_bytes = 0
                last_sample_t = time.time()
                last_sample_uploaded = 0
                smoothed_speed = 0.0

                def _report_progress():
                    nonlocal last_sample_t, last_sample_uploaded, smoothed_speed
                    now = time.time()
                    sample_dt = now - last_sample_t
                    if sample_dt >= 0.5:
                        inst = (uploaded_bytes - last_sample_uploaded) / sample_dt
                        alpha = 0.3
                        smoothed_speed = inst if smoothed_speed == 0 else (
                            alpha * inst + (1 - alpha) * smoothed_speed)
                        last_sample_t = now
                        last_sample_uploaded = uploaded_bytes
                    pct = int(uploaded_bytes / total_upload_bytes * 100) if total_upload_bytes else 100
                    bytes_remaining = total_upload_bytes - uploaded_bytes
                    eta = _fmt_eta(bytes_remaining / smoothed_speed) if smoothed_speed > 0 else "?"
                    self.progress.emit(
                        int(uploaded_bytes), int(total_upload_bytes),
                        f"{pct}% — ⬆ {smoothed_speed / 1024 / 1024:.1f} MB/s — осталось: {eta}"
                    )

                if self.disk_mode == "ssd":
                    # SSD: параллельная заливка (UPLOAD_WORKERS_PACKS_SSD
                    # потоков) — диск не боится конкурентной записи в
                    # разные файлы, узкое место — сеть/CPU. requests.Session
                    # у dav (см. NextcloudDAV/PanelHTTP) уже настроен под
                    # конкурентные запросы (тот же паттерн, что и per-chunk
                    # протокол execute_sync() выше, тоже параллельный через
                    # тот же dav).
                    # Ошибка захватывается ЗДЕСЬ (dav.last_error читается
                    # сразу после своего put_file(), в том же вызове) — а
                    # не в главном потоке после future.result(), потому что
                    # dav один на все потоки-загрузчики: если читать
                    # last_error снаружи, к этому моменту его может уже
                    # перезаписать ДРУГОЙ, ещё выполняющийся future.
                    def _upload_one_pack(pp: Path) -> Tuple[str, bool, int, str]:
                        ok = self._retry_until_recovered(
                            lambda: dav.put_file(self._rp(PACKS_DIR, pp.name), pp),
                            dav, stop_fn, pp.name,
                        )
                        return pp.name, ok, pp.stat().st_size, dav.last_error

                    with ThreadPoolExecutor(max_workers=UPLOAD_WORKERS_PACKS_SSD) as pool:
                        futures = {pool.submit(_upload_one_pack, pp): pp for pp in pw.pack_paths}
                        for future in as_completed(futures):
                            if stop_fn and stop_fn():
                                pool.shutdown(wait=False, cancel_futures=True)
                                return False, "Остановлено пользователем"
                            name, ok, pack_size, err = future.result()
                            if not ok:
                                pool.shutdown(wait=False, cancel_futures=True)
                                return False, f"Не удалось загрузить {name}: {err or 'см. лог'}"
                            uploaded_bytes += pack_size
                            self.log.emit(f"  ✅ {name} ({pack_size // 1024} KB)")
                            _report_progress()
                else:
                    # HDD: строго последовательно, один PUT за раз — см.
                    # CLAUDE.md "Алгоритм заливки под SSD/HDD" за полное
                    # обоснование (random-write penalty на вращающемся
                    # диске при параллельной записи в разные файлы).
                    for pack_path in pw.pack_paths:
                        if stop_fn and stop_fn():
                            return False, "Остановлено пользователем"
                        ok = self._retry_until_recovered(
                            lambda: dav.put_file(self._rp(PACKS_DIR, pack_path.name), pack_path),
                            dav, stop_fn, pack_path.name,
                        )
                        if not ok:
                            return False, f"Не удалось загрузить {pack_path.name}: {dav.last_error or 'см. лог'}"
                        pack_size = pack_path.stat().st_size
                        uploaded_bytes += pack_size
                        self.log.emit(f"  ✅ {pack_path.name} ({pack_size // 1024} KB)")
                        _report_progress()

                merged = {**existing_index, **pw.locations}
                index_path = pack_dir / CHUNK_INDEX_NAME
                write_chunk_index_db(index_path, merged)
                ok = self._retry_until_recovered(
                    lambda: dav.put_file(self._rp(CHUNK_INDEX_NAME), index_path),
                    dav, stop_fn, "chunk_index.db",
                )
                if not ok:
                    return False, f"Не удалось загрузить chunk_index.db: {dav.last_error or 'см. лог'}"
                self.log.emit(f"📋 chunk_index.db обновлён: {len(merged)} чанков всего")
            else:
                self.log.emit("ℹ️  Новых чанков нет")
        finally:
            shutil.rmtree(pack_dir, ignore_errors=True)

        self.log.emit(f"📁 Сохраняем версию {new_manifest.build_id}…")
        version_json = new_manifest.to_json().encode("utf-8")
        self._retry_until_recovered(
            lambda: dav.put(
                self._rp(VERSIONS_DIR, f"{new_manifest.build_id}.json"),
                version_json,
                content_type="application/json",
            ),
            dav, stop_fn, f"{new_manifest.build_id}.json",
        )

        self.log.emit("📝 Обновляем depot.json…")
        depot_meta = self._build_depot_meta(new_manifest)
        # storage_format — единственное различие в depot.json между двумя
        # протоколами; читающая сторона (панель/будущий launcher-reader)
        # смотрит на это поле, чтобы понять, брать ли чанк из chunks/<xx>/<id>
        # или искать его в chunk_index.db + packs/.
        depot_meta["storage_format"] = "packed"
        self._retry_until_recovered(
            lambda: dav.put(
                self._rp(DEPOT_META),
                json.dumps(depot_meta, ensure_ascii=False, indent=2).encode("utf-8"),
                content_type="application/json",
            ),
            dav, stop_fn, "depot.json",
        )

        self.log.emit("📤 Публикуем depot_manifest.json…")
        manifest_json = new_manifest.to_json().encode("utf-8")
        ok = self._retry_until_recovered(
            lambda: dav.put(
                self._rp("depot_manifest.json"),
                manifest_json,
                content_type="application/json",
            ),
            dav, stop_fn, "depot_manifest.json",
        )
        if not ok:
            return False, f"Критическая ошибка: не удалось обновить depot_manifest.json: {dav.last_error or 'см. лог'}"

        msg = (
            f"✅ Build #{new_manifest.build_number} опубликован (packed)! "
            f"({new_manifest.build_id})  "
            f"Новых чанков упаковано: {total}, "
            f"новых/изменённых файлов: {delta.total_new_files}"
        )
        return True, msg

    @staticmethod
    def _build_depot_meta(m: DepotManifest) -> dict:
        return {
            "app_id":   m.app_id,
            "depot_id": m.depot_id,
            "channels": {
                m.channel: {
                    "build_number": m.build_number,
                    "build_id":     m.build_id,
                    "created_at":   m.created_at,
                    "file_count":   len(m.files),
                    "total_size":   m.total_size,
                    "manifest":     "depot_manifest.json",
                    "description":  m.description,
                }
            },
            "updated_at": datetime.now().isoformat(),
        }

    def close(self):
        if self._dav:
            self._dav.close()
            self._dav = None


# ── Worker Thread ─────────────────────────────────────────────────────────────

class DepotBuildWorker(ThreadSafeWorker):
    """
    QThread worker — полный цикл сборки и публикации депо.
    """

    scan_done = pyqtSignal(object, object, object)
    finished  = pyqtSignal(bool, str)
    progress  = pyqtSignal(int, int, str)
    log       = pyqtSignal(str)

    def __init__(
        self,
        config:             dict,
        confirmed_delta:    Optional[DepotDelta]    = None,
        confirmed_manifest: Optional[DepotManifest] = None,
    ):
        super().__init__()
        self.config             = config
        self.confirmed_delta    = confirmed_delta
        self.confirmed_manifest = confirmed_manifest

    def run(self):
        try:
            cfg  = self.config
            sync = DepotSyncManager(cfg)
            sync.log.connect(self.log)
            sync.progress.connect(self.progress)

            chunk_size = cfg.get("depot", {}).get("chunk_size", DEFAULT_CHUNK_SIZE)
            cm = ChunkManager(chunk_size=chunk_size)
            cm.log.connect(self.log)
            cm.progress.connect(self.progress)

            if self.confirmed_delta is not None:
                self.log.emit("🚀 Начинаем загрузку чанков на сервер…")
                if not sync.ensure_depot_structure():
                    self.finished.emit(False, "Не удалось создать структуру депо")
                    return
                # use_packs — см. CLAUDE.md "Упаковка чанков в pack-файлы":
                # публикует чанки как pack-файлы + chunk_index.db вместо
                # chunks/<xx>/<id> по одному, старое поведение остаётся
                # умолчанием (флаг выключен), пока читающая сторона
                # (панель/лаунчер) не подтверждена рабочей.
                sync_fn = sync.execute_sync_packed if sync.use_packs else sync.execute_sync
                # components (dict {name: {local_dir, included, excludes}}) —
                # актуальный путь через DepotTab (Skyrim/MO2p/MO2ext, см.
                # CLAUDE.md "Компонентная модель для Депо (chunks)").
                # local_dir (строка) — старый однопапочный путь, оставлен
                # ради publish_packed.py (CLI без GUI) и обратной
                # совместимости уже сохранённых конфигов.
                components = cfg.get("components")
                source = (
                    {name: c["local_dir"] for name, c in components.items()
                     if c.get("included") and c.get("local_dir")}
                    if components else cfg.get("local_dir", "")
                )
                ok, msg = sync_fn(
                    new_manifest = self.confirmed_manifest,
                    delta        = self.confirmed_delta,
                    local_dir    = source,
                    stop_fn      = self._should_stop,
                    pause_fn     = self._wait_if_paused,
                )
                self.finished.emit(ok, msg)
                return

            self.log.emit("🔌 Проверяем соединение…")
            ok, msg = sync.test_connection()
            if not ok:
                self.finished.emit(False, f"Ошибка подключения: {msg}")
                return
            self.log.emit(f"✅ {msg}")

            self.log.emit("📥 Загружаем текущий манифест с сервера…")
            prev_manifest = sync.fetch_remote_manifest()
            if prev_manifest:
                self.log.emit(
                    f"📋 Сервер: build #{prev_manifest.build_number} "
                    f"({prev_manifest.build_id}), "
                    f"файлов: {len(prev_manifest.files)}, "
                    f"размер: {prev_manifest.human_size()}"
                )
            else:
                self.log.emit("ℹ️  Манифест не найден — первая публикация")

            remote_chunks = sync.fetch_remote_chunk_ids(prev_manifest)

            components = cfg.get("components")
            if components:
                comp_summary = ", ".join(
                    f"{n}: {c['local_dir']}" for n, c in components.items()
                    if c.get("included") and c.get("local_dir")
                )
                self.log.emit(f"📁 Сканируем компоненты: {comp_summary}")
                entries, stats, component_roots = cm.scan_components(
                    components    = components,
                    prev_manifest = prev_manifest,
                )
                excludes  = []   # исключения теперь per-компонент, не общие
                local_dir_display = ", ".join(component_roots.values())
            else:
                # Старый однопапочный путь — оставлен ради publish_packed.py
                # (CLI без GUI) и уже сохранённых конфигов без "components".
                local_dir = cfg.get("local_dir", "")
                excludes  = cfg.get("excludes", [])
                self.log.emit(f"📁 Сканируем: {local_dir}")
                entries, stats = cm.scan_directory(
                    local_dir     = Path(local_dir),
                    excludes      = excludes,
                    prev_manifest = prev_manifest,
                )
                local_dir_display = local_dir

            if not entries:
                self.finished.emit(False, "Нет файлов для загрузки")
                return

            self.log.emit(f"📊 {stats}")

            depot_cfg = cfg.get("depot", {})
            new_manifest = DepotManifest(
                app_id       = depot_cfg.get("app_id", "app"),
                depot_id     = depot_cfg.get("depot_id", 1),
                channel      = depot_cfg.get("channel", "stable"),
                chunk_size   = chunk_size,
                build_number = (prev_manifest.build_number + 1) if prev_manifest else 1,
            )
            new_manifest.files     = entries
            new_manifest.excludes  = excludes
            new_manifest.local_dir = local_dir_display

            prev_entries = prev_manifest.files if prev_manifest else {}
            delta = cm.compute_delta(entries, prev_entries, remote_chunks)
            self.log.emit(f"📋 Delta: {delta.summary()}")

            self.scan_done.emit(new_manifest, delta, prev_manifest)

        except Exception as e:
            import traceback
            self.log.emit(f"❌ {e}\n{traceback.format_exc()}")
            self.finished.emit(False, str(e))