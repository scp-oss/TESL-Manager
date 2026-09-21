# ==================== recover_from_chunks.py ====================
"""
Восстановление утраченной сборки из chunk-based депо на WebDAV.

СИТУАЦИЯ (см. CLAUDE.md, "Восстановление сборки из чанков — приоритет №1"):
локальные исходники минимум одной опубликованной сборки утрачены на машине
оператора. Единственная оставшаяся копия — чанки на сервере
(`chunks/<xx>/<hash>` + `versions/<key>.json` + `depot.json`). Этот скрипт
их скачивает, собирает файлы обратно и верифицирует по sha256 — БЕЗ Qt,
можно гонять с любого ПК с Python 3.9+ и requests.

Схема манифеста версии подтверждена на реальных данных сервера
(`versions/v1_20eb01df.json`, 2026-09-21) — это ГИБРИД: chunk-хранилище
физически, но структура манифеста версии — компонентная (`components`:
`Skyrim`/`MO2p`/`MO2ext`, каждый — `{"included": bool, "files": {...}}`),
как в `release_manager.py`, а не плоский `files` верхнего уровня, который
ожидает чистый `chunk_manager.py`/`launcher_client.py`. Поэтому
`parse_version_manifest()` ниже понимает ОБА варианта — на случай, если
другая версия/другая сборка когда-нибудь окажется в чистом chunk-формате.

Поля файла (`FileEntry`) — `{"size": int, "hash": "<sha256 файла>",
"chunks": [{"id": "<sha256 чанка>", "offset": int, "size": int}, ...]}` —
СОВПАДАЮТ буквально с `chunk_manager.py::FileEntry`/`ChunkInfo.to_dict()`,
подтверждено на реальных данных, адаптация имён полей не понадобилась.

ИСПОЛЬЗОВАНИЕ (ценность данных — единственная копия, поэтому: сначала
ВСЕГДА --dry-run, читай отчёт, и только потом реальная закачка):

    python recover_from_chunks.py \\
        --server https://nethunter.sytes.net/cloud/remote.php/dav/files/SkyrimDownloader \\
        --user SkyrimDownloader \\
        --remote-path 1TB/TESS/Instances/TESVAE \\
        --out-dir D:/Recovery/TESVAE \\
        --version-key v1_20eb01df \\
        --dry-run

    # пароль не передаём в CLI (виден в истории/списке процессов) —
    # скрипт спросит его через getpass, либо возьми из переменной
    # окружения TESL_DAV_PASSWORD:
    #   set TESL_DAV_PASSWORD=...   (cmd)  /  $env:TESL_DAV_PASSWORD="..." (PowerShell)

    # когда отчёт устраивает — реальная закачка (можно прерывать и
    # перезапускать: --resume включён по умолчанию, пропускает файлы,
    # уже верно собранные на диске):
    python recover_from_chunks.py ... --version-key v1_20eb01df

--version-key можно не указывать — тогда скрипт попробует определить
версию сам из depot.json. РЕАЛЬНАЯ схема depot.json (подтверждено на живом
сервере 2026-09-21) — `{"versions": {key: {...,"manifest": "versions/key.json"}},
"current": {channel: key} | {}}`; если `current` пуст (версия ещё не
активирована ни на одном канале — ровно так на сервере прямо сейчас) и
`versions` содержит ровно одну запись, эта версия берётся автоматически.
Два более старых варианта схемы depot.json (компонентный `current`-only,
chunk-формат `channels`) оставлены как запасные варианты на случай другой
сборки — см. `resolve_version()`. Раз ключ уже известен
(`v1_20eb01df`), надёжнее передать его явно через `--version-key`.

ЛОКАЛЬНЫЙ ИНДЕКС (SQLite, `db.py`): по умолчанию каждый успешный запуск
(и `--dry-run`, и реальное восстановление) заносит разобранный манифест
версии (файлы/чанки) и итоги запуска в локальную БД оператора
(`%APPDATA%\\Uploder\\uploder.db`, см. `db.py`'s собственный докстринг —
это ТОЛЬКО локальный индекс для запросов, не публикуется на WebDAV и не
читается TESL). Отключить — `--no-db`; свой путь к файлу БД — `--db-path`.
"""

import argparse
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.auth import HTTPBasicAuth

# db.py лежит рядом в этой же папке (depot_sync_manager/) — добавляем её в
# sys.path явно, чтобы скрипт можно было запускать и как файл напрямую
# (`python recover_from_chunks.py`), не только как часть пакета.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import db as _db  # noqa: E402 (см. комментарий выше про sys.path)

CHUNK_DIR    = "chunks"
VERSIONS_DIR = "versions"
DEPOT_META   = "depot.json"


# ── Модели манифеста ─────────────────────────────────────────────────────────

@dataclass
class ChunkInfo:
    chunk_id: str
    offset:   int
    size:     int


@dataclass
class FileEntry:
    path:      str   # относительный путь внутри компонента (или полный, если схема плоская)
    component: str   # имя компонента ("Skyrim"/"MO2p"/"MO2ext"), "" если схема плоская
    size:      int
    file_hash: str
    chunks:    List[ChunkInfo]

    @classmethod
    def from_dict(cls, path: str, component: str, d: dict) -> "FileEntry":
        return cls(
            path=path,
            component=component,
            size=d["size"],
            file_hash=d["hash"],
            chunks=[
                ChunkInfo(c["id"], c["offset"], c["size"])
                for c in d.get("chunks", [])
            ],
        )

    @property
    def rel_out_path(self) -> str:
        return f"{self.component}/{self.path}" if self.component else self.path

    def chunk_ids(self) -> List[str]:
        return [c.chunk_id for c in self.chunks]


def parse_version_manifest(data: dict) -> List[FileEntry]:
    """
    Понимает ОБА варианта схемы манифеста версии:
      1. Компонентная (подтверждено на реальном сервере, v1_20eb01df.json):
         {"components": {"Skyrim": {"included": true, "files": {path: {...}}},
                          "MO2p": {...}, "MO2ext": {"included": false, "files": null}}}
         Компоненты с included=false или без files пропускаются целиком —
         значит их и не было в этой публикации, восстанавливать нечего.
      2. Плоская chunk-протокола (`chunk_manager.py::DepotManifest`):
         {"files": {path: {...}}}
    """
    entries: List[FileEntry] = []

    if "components" in data and data["components"]:
        for comp_name, comp in data["components"].items():
            if not comp or not comp.get("included", True):
                continue
            files = comp.get("files") or {}
            for path, info in files.items():
                entries.append(FileEntry.from_dict(path, comp_name, info))
    elif "files" in data and data["files"]:
        for path, info in data["files"].items():
            entries.append(FileEntry.from_dict(path, "", info))
    else:
        raise ValueError(
            "Не нашли ни непустого 'components', ни непустого 'files' на "
            "верхнем уровне манифеста версии — схема не распознана, "
            "не гадаем дальше. Проверь версию вручную."
        )

    return entries


# ── Recoverer ─────────────────────────────────────────────────────────────────

class ChunkRecoverer:
    """
    Скачивает чанки, собирает файлы, верифицирует. Логика чанк-скачивания и
    сборки — тот же цикл, что в `launcher_client.py::DepotClient.update()`
    (переиспользован, не переписан с нуля), адаптированный под:
      - явный выбор конкретной версии по ключу (а не "текущий манифест канала"),
      - компонентную структуру реального манифеста,
      - dry-run с проверкой наличия чанков на сервере ПЕРЕД любой закачкой,
      - resume: пропуск уже верно собранных файлов при повторном запуске.
    """

    def __init__(
        self,
        server_url:  str,
        username:    str,
        password:    str,
        remote_path: str,
        out_dir:     str,
        verify_ssl:  bool = True,
        max_workers: int  = 8,
        on_log=print,
    ):
        self.base_url    = server_url.rstrip("/")
        self.remote_path = remote_path.strip("/")
        self.out_dir     = Path(out_dir)
        self.max_workers = max_workers
        self.on_log      = on_log

        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(username, password)
        self.session.verify = verify_ssl
        self.session.headers["User-Agent"] = "TESL-Manager-Recovery/1.0"

    # ── URL helpers ───────────────────────────────────────────────────────────

    def _url(self, *parts: str) -> str:
        segs = [self.base_url, self.remote_path] + [p.strip("/") for p in parts if p]
        return "/".join(segs)

    def _chunk_url(self, chunk_id: str) -> str:
        return self._url(CHUNK_DIR, chunk_id[:2], chunk_id)

    # ── JSON fetch ────────────────────────────────────────────────────────────

    def fetch_json(self, *parts: str) -> Optional[dict]:
        url = self._url(*parts)
        try:
            r = self.session.get(url, timeout=60)
        except Exception as e:
            self.on_log(f"❌ Ошибка GET {url}: {e}")
            return None
        if r.status_code != 200:
            self.on_log(f"⚠️ HTTP {r.status_code}: {url}")
            return None
        try:
            return r.json()
        except Exception as e:
            self.on_log(f"❌ Ошибка парсинга JSON {url}: {e}")
            return None

    # ── Version resolution ───────────────────────────────────────────────────

    def resolve_version(self, version_key: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[dict]]:
        """
        Возвращает (путь_к_манифесту, resolved_version_key, version_meta).
        `version_meta` — запись из depot.json для этой версии (если найдена
        оттуда), с полями вроде `build_number`/`build_id`/`channel`/
        `file_count`/`total_size`/`chunk_count`/`components` — None, если
        версия задана явным `--version-key` и depot.json не запрашивался.
        """
        if version_key:
            return f"{VERSIONS_DIR}/{version_key}.json", version_key, None

        self.on_log("📥 --version-key не задан, пробуем определить из depot.json...")
        depot = self.fetch_json(DEPOT_META)
        if depot is None:
            return None, None, None

        # РЕАЛЬНАЯ схема, подтверждена на живом сервере 2026-09-21:
        # {"versions": {key: {..., "manifest": "versions/key.json", "components": {...}}},
        #  "current": {channel: key} | {}}
        versions = depot.get("versions")
        if isinstance(versions, dict) and versions:
            current = depot.get("current") or {}
            key = None
            if isinstance(current, dict) and current:
                key = next(iter(current.values()))
            elif len(versions) == 1:
                key = next(iter(versions.keys()))
                self.on_log(
                    f"⚠️ 'current' в depot.json пуст (версия ещё не активирована ни на одном "
                    f"канале) — берём единственную версию из 'versions': {key}"
                )
            else:
                self.on_log(
                    f"❌ 'current' в depot.json пуст, а версий несколько "
                    f"({list(versions.keys())}) — укажи --version-key явно"
                )
                return None, None, None

            meta = versions.get(key)
            if meta is None:
                self.on_log(f"❌ 'current'/единственная версия указывает на {key}, которой нет в 'versions'")
                return None, None, None
            manifest_rel = meta.get("manifest") or f"{VERSIONS_DIR}/{key}.json"
            self.on_log(f"📄 depot.json ('versions'-формат): версия {key} -> {manifest_rel}")
            return manifest_rel, key, meta

        # Компонентный формат (более старая гипотеза, оставлена на случай
        # другой сборки): {"current": {channel: version_key}}
        current = depot.get("current")
        if isinstance(current, dict) and current:
            key = next(iter(current.values()))
            self.on_log(f"📄 depot.json (компонентный формат): версия {key}")
            return f"{VERSIONS_DIR}/{key}.json", key, None

        # Chunk-формат (тоже гипотеза): {"channels": {channel: {build_number, build_id}}}
        channels = depot.get("channels")
        if isinstance(channels, dict) and channels:
            ch = next(iter(channels.values()))
            build_id     = ch.get("build_id")
            build_number = ch.get("build_number")
            if build_id and build_number:
                key = f"v{build_number}_{build_id}"
                self.on_log(f"📄 depot.json (chunk-формат): версия {key}")
                return f"{VERSIONS_DIR}/{key}.json", key, None
            if build_id:
                self.on_log(f"📄 depot.json (chunk-формат, без build_number): версия {build_id}")
                return f"{VERSIONS_DIR}/{build_id}.json", build_id, None

        self.on_log("❌ Не удалось определить версию из depot.json — укажи --version-key явно")
        return None, None, None

    def fetch_version_manifest(
        self, version_key: Optional[str]
    ) -> Tuple[Optional[str], Optional[str], Optional[List[FileEntry]], Optional[dict], Optional[dict]]:
        """
        Возвращает (путь, resolved_version_key, entries, raw_version_json, depot_version_meta).
        """
        path, resolved_key, depot_meta = self.resolve_version(version_key)
        if path is None:
            return None, None, None, None, None
        self.on_log(f"📥 Загружаем {path} ...")
        data = self.fetch_json(path)
        if data is None:
            return None, resolved_key, None, None, depot_meta
        try:
            entries = parse_version_manifest(data)
        except ValueError as e:
            self.on_log(f"❌ {e}")
            return None, resolved_key, None, None, depot_meta
        return path, resolved_key, entries, data, depot_meta

    # ── Hashing ───────────────────────────────────────────────────────────────

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    # ── Chunk existence check (для dry-run, без скачивания тела) ───────────────

    def check_chunk_exists(self, chunk_id: str) -> bool:
        url = self._chunk_url(chunk_id)
        try:
            r = self.session.head(url, timeout=30)
            if r.status_code == 200:
                return True
            if r.status_code in (403, 405):
                # Некоторые WebDAV-сервера не отдают ожидаемый ответ на HEAD —
                # подстраховываемся мини-GET с Range на 1 байт.
                r2 = self.session.get(url, timeout=30, headers={"Range": "bytes=0-0"})
                return r2.status_code in (200, 206)
            return False
        except Exception:
            return False

    # ── Chunk download ────────────────────────────────────────────────────────

    def _download_chunk(self, chunk_id: str) -> Optional[bytes]:
        url = self._chunk_url(chunk_id)
        try:
            r = self.session.get(url, timeout=180)
        except Exception as e:
            self.on_log(f"❌ Ошибка GET чанка {chunk_id[:12]}: {e}")
            return None
        if r.status_code != 200:
            self.on_log(f"⚠️ HTTP {r.status_code} для чанка {chunk_id[:12]}")
            return None
        data = r.content
        actual = self._hash_bytes(data)
        if actual != chunk_id:
            self.on_log(f"❌ Чанк повреждён: {chunk_id[:12]} (сервер вернул {actual[:12]})")
            return None
        return data

    # ── Dry-run ───────────────────────────────────────────────────────────────

    def dry_run(self, entries: List[FileEntry], check_server: bool = True) -> dict:
        total_bytes = sum(e.size for e in entries)
        chunk_ids: Set[str] = set()
        for e in entries:
            chunk_ids.update(e.chunk_ids())

        by_component: Dict[str, int] = {}
        for e in entries:
            by_component[e.component or "(без компонентов)"] = (
                by_component.get(e.component or "(без компонентов)", 0) + 1
            )

        self.on_log(
            f"📊 Файлов: {len(entries)}, уникальных чанков: {len(chunk_ids)}, "
            f"всего байт: {total_bytes:,} ({total_bytes / (1024**3):.2f} GiB)"
        )
        for comp, count in sorted(by_component.items()):
            self.on_log(f"   {comp}: {count} файлов")

        report = {
            "files": len(entries),
            "unique_chunks": len(chunk_ids),
            "total_bytes": total_bytes,
            "missing_chunks": [],
        }

        if check_server:
            self.on_log(f"🔍 Проверяем наличие {len(chunk_ids)} чанков на сервере (без скачивания тела)...")
            missing: List[str] = []
            checked = 0
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {pool.submit(self.check_chunk_exists, cid): cid for cid in chunk_ids}
                for future in as_completed(futures):
                    cid = futures[future]
                    checked += 1
                    if checked % 200 == 0 or checked == len(chunk_ids):
                        self.on_log(f"   ...{checked}/{len(chunk_ids)}")
                    try:
                        exists = future.result()
                    except Exception:
                        exists = False
                    if not exists:
                        missing.append(cid)

            report["missing_chunks"] = missing
            if missing:
                self.on_log(f"❌ ОТСУТСТВУЮТ на сервере: {len(missing)} чанков из {len(chunk_ids)}")
                for cid in missing[:20]:
                    self.on_log(f"    - {cid}")
                if len(missing) > 20:
                    self.on_log(f"    ... и ещё {len(missing) - 20}")
            else:
                self.on_log("✅ Все чанки присутствуют на сервере — можно запускать без --dry-run")

        return report

    # ── Recover ───────────────────────────────────────────────────────────────

    def recover(self, entries: List[FileEntry], resume: bool = True) -> dict:
        """
        Скачивает все чанки, собирает файлы, верифицирует sha256 целиком.
        resume=True — пропускает файлы, уже существующие локально с верным
        размером И хэшем (можно безопасно прерывать Ctrl+C и перезапускать
        тем же способом — единственная копия этих данных, лучше перебдеть).

        Возвращает словарь статистики (files_total/files_ok/files_failed/
        files_skipped/chunks_total/chunks_missing/ok) — используется и для
        exit-кода CLI, и для записи в локальный индекс (`db.py`).
        """
        to_process: List[FileEntry] = []
        for e in entries:
            out_path = self.out_dir / e.rel_out_path
            if resume and out_path.exists() and out_path.stat().st_size == e.size:
                if self._hash_file(out_path) == e.file_hash:
                    continue
            to_process.append(e)

        skipped = len(entries) - len(to_process)
        if skipped:
            self.on_log(f"⏭ Пропускаем {skipped} уже верно восстановленных файлов")

        if not to_process:
            self.on_log("✅ Восстанавливать нечего — все файлы уже на месте и верны")
            return {
                "ok": True, "files_total": len(entries), "files_ok": 0,
                "files_failed": 0, "files_skipped": skipped,
                "chunks_total": 0, "chunks_missing": 0,
            }

        needed_chunks: Set[str] = set()
        chunk_to_files: Dict[str, List[FileEntry]] = {}
        for e in to_process:
            for c in e.chunks:
                needed_chunks.add(c.chunk_id)
                chunk_to_files.setdefault(c.chunk_id, []).append(e)

        self.on_log(f"⬇️ Качаем {len(needed_chunks)} уникальных чанков для {len(to_process)} файлов...")

        chunk_cache: Dict[str, bytes] = {}
        failed_chunks: Set[str] = set()
        done = 0

        def _dl(cid: str) -> Tuple[str, Optional[bytes]]:
            return cid, self._download_chunk(cid)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(_dl, cid): cid for cid in needed_chunks}
            for future in as_completed(futures):
                cid, data = future.result()
                done += 1
                if done % 100 == 0 or done == len(needed_chunks):
                    self.on_log(f"   ...{done}/{len(needed_chunks)}")
                if data is not None:
                    chunk_cache[cid] = data
                else:
                    failed_chunks.add(cid)

        if failed_chunks:
            broken_files = set()
            for cid in failed_chunks:
                for e in chunk_to_files.get(cid, []):
                    broken_files.add(e.rel_out_path)
            self.on_log(
                f"❌ Не скачали {len(failed_chunks)} чанков — под угрозой "
                f"{len(broken_files)} файлов, они НЕ будут собраны в этом запуске"
            )

        self.on_log("🔧 Собираем файлы из чанков...")
        ok, fail = 0, 0
        for e in to_process:
            missing = [c.chunk_id for c in e.chunks if c.chunk_id not in chunk_cache]
            if missing:
                self.on_log(f"⚠️ Пропускаем {e.rel_out_path} — недостаёт {len(missing)} чанков")
                fail += 1
                continue

            out_path = self.out_dir / e.rel_out_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(out_path, "wb") as f:
                    for c in sorted(e.chunks, key=lambda c: c.offset):
                        f.write(chunk_cache[c.chunk_id])
            except Exception as ex:
                self.on_log(f"❌ Ошибка записи {e.rel_out_path}: {ex}")
                fail += 1
                continue

            actual_hash = self._hash_file(out_path)
            if actual_hash != e.file_hash:
                self.on_log(
                    f"❌ Верификация провалена: {e.rel_out_path} "
                    f"(ожидали {e.file_hash[:12]}, получили {actual_hash[:12]})"
                )
                fail += 1
                continue

            ok += 1

        self.on_log(f"{'✅' if fail == 0 else '⚠️'} Готово. Собрано и верифицировано: {ok}, ошибок: {fail}")
        if fail:
            self.on_log(
                "⚠️ Часть файлов НЕ восстановлена (недостающие чанки на сервере "
                "или ошибка верификации) — перезапуск с --resume (по умолчанию) "
                "докачает только их после устранения причины."
            )
        return {
            "ok": fail == 0, "files_total": len(entries), "files_ok": ok,
            "files_failed": fail, "files_skipped": skipped,
            "chunks_total": len(needed_chunks), "chunks_missing": len(failed_chunks),
        }

    def close(self):
        self.session.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def _resolve_password(cli_password: Optional[str]) -> str:
    if cli_password:
        return cli_password
    env_pw = os.getenv("TESL_DAV_PASSWORD")
    if env_pw:
        return env_pw
    return getpass.getpass("WebDAV пароль (SkyrimDownloader / read-аккаунт): ")


def main():
    parser = argparse.ArgumentParser(
        description="Восстановление утраченной сборки из chunk-based депо на WebDAV.",
    )
    parser.add_argument("--server", required=True, help="Базовый URL WebDAV (без remote-path)")
    parser.add_argument("--user", required=True, help="WebDAV-логин")
    parser.add_argument(
        "--password", default=None,
        help="WebDAV-пароль. НЕ передавай в общем терминале — по умолчанию "
             "скрипт берёт TESL_DAV_PASSWORD из окружения или спросит через getpass.",
    )
    parser.add_argument(
        "--remote-path", required=True,
        help="Путь к папке сборки на WebDAV, где лежат chunks/, versions/, depot.json "
             "(например 1TB/TESS/Instances/TESVAE)",
    )
    parser.add_argument("--out-dir", required=True, help="Локальная папка, куда собирать файлы")
    parser.add_argument(
        "--version-key", default=None,
        help="Ключ версии без .json (например v1_20eb01df). Если не задан — "
             "скрипт попробует определить его из depot.json.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Только отчёт: сколько файлов/чанков/байт, есть ли все чанки на "
             "сервере. НИЧЕГО не скачивает и не пишет на диск. Запускай ПЕРВЫМ.",
    )
    parser.add_argument(
        "--no-check-server", action="store_true",
        help="В --dry-run не проверять наличие чанков на сервере (быстрее, но "
             "менее полезно) — по умолчанию проверка включена.",
    )
    parser.add_argument(
        "--no-resume", action="store_true",
        help="Не пропускать уже верно восстановленные файлы — перекачать всё заново.",
    )
    parser.add_argument("--workers", type=int, default=8, help="Потоков параллельной загрузки чанков")
    parser.add_argument("--no-ssl-verify", action="store_true", help="Отключить проверку TLS-сертификата")
    parser.add_argument(
        "--no-db", action="store_true",
        help="Не писать в локальный индекс (db.py, %%APPDATA%%/Uploder/uploder.db). "
             "По умолчанию манифест версии и итоги запуска заносятся туда автоматически.",
    )
    parser.add_argument(
        "--db-path", default=None,
        help="Свой путь к файлу SQLite вместо %%APPDATA%%/Uploder/uploder.db.",
    )
    args = parser.parse_args()

    password = _resolve_password(args.password)

    recoverer = ChunkRecoverer(
        server_url=args.server,
        username=args.user,
        password=password,
        remote_path=args.remote_path,
        out_dir=args.out_dir,
        verify_ssl=not args.no_ssl_verify,
        max_workers=args.workers,
    )

    conn = None
    if not args.no_db:
        try:
            conn = _db.init_db(args.db_path)
        except Exception as e:
            print(f"⚠️ Не удалось открыть локальный индекс ({e}) — продолжаем без него.")
            conn = None

    try:
        version_path, version_key, entries, raw_manifest, depot_meta = recoverer.fetch_version_manifest(args.version_key)
        if entries is None:
            print("❌ Не удалось загрузить/разобрать манифест версии — см. лог выше.")
            sys.exit(1)

        print(f"📋 Манифест версии: {version_path}")

        if conn is not None and version_key:
            protocol = "hybrid" if any(e.component for e in entries) else "chunk"
            depot_meta = depot_meta or {}
            _db.sync_release(
                conn, version_key=version_key, protocol=protocol, entries=entries,
                raw_manifest=raw_manifest, remote_path=args.remote_path,
                build_number=depot_meta.get("build_number"),
                build_id=depot_meta.get("build_id"),
                channel=depot_meta.get("channel"),
                declared_chunk_count=depot_meta.get("chunk_count"),
            )
            print(f"🗄 Манифест версии занесён в локальный индекс ({len(entries)} файлов).")

        mode = "dry-run" if args.dry_run else "recover"
        run_id = _db.start_recovery_run(conn, version_key, args.remote_path, mode) if conn is not None else None

        if args.dry_run:
            report = recoverer.dry_run(entries, check_server=not args.no_check_server)
            print("\nℹ️ Это был --dry-run — ничего не скачано и не записано на диск.")
            if conn is not None and run_id is not None:
                _db.finish_recovery_run(conn, run_id, _db.RecoveryRunResult(
                    version_key=version_key, remote_path=args.remote_path, mode=mode,
                    files_total=report["files"], chunks_total=report["unique_chunks"],
                    chunks_missing=len(report["missing_chunks"]),
                    notes="dry-run, ничего не скачивалось",
                ))
            sys.exit(0)

        stats = recoverer.recover(entries, resume=not args.no_resume)
        if conn is not None and run_id is not None:
            _db.finish_recovery_run(conn, run_id, _db.RecoveryRunResult(
                version_key=version_key, remote_path=args.remote_path, mode=mode,
                files_total=stats["files_total"], files_ok=stats["files_ok"],
                files_failed=stats["files_failed"], files_skipped=stats["files_skipped"],
                chunks_total=stats["chunks_total"], chunks_missing=stats["chunks_missing"],
                out_dir=args.out_dir,
            ))
        sys.exit(0 if stats["ok"] else 1)
    finally:
        recoverer.close()
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
