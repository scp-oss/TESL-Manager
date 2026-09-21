# ==================== launcher_client.py ====================
"""
Launcher Client — клиентская часть depot-системы.
Скачивает только изменившиеся чанки, собирает файлы, верифицирует.

Этот файл — ОТДЕЛЬНЫЙ проект (не часть Uploder).
Зависимости: requests, tqdm (опционально)

Использование:
    client = DepotClient(
        server_url   = "https://nethunter.sytes.net/cloud/remote.php/dav/files/admin",
        username     = "launcher_user",
        password     = "read_only_token",
        remote_path  = "1TB/depot/skyrim-001",
        local_dir    = "C:/Games/Skyrim",
        verify_ssl   = True,
    )
    client.update(verify_after=True)
"""

import os
import json
import hashlib
import threading
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Callable
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.auth import HTTPBasicAuth

# ── Реиспользуем модели из chunk_manager ─────────────────────────────────────
# В реальном проекте лаунчера — скопировать нужные классы сюда
# или вынести в shared пакет.
# Для демонстрации дублируем минимальный набор.


CHUNK_DIR    = "chunks"
DEPOT_META   = "depot.json"
MANIFEST_FILE = "depot_manifest.json"


@dataclass
class ChunkInfo:
    chunk_id: str
    offset:   int
    size:     int


@dataclass
class FileEntry:
    path:      str
    size:      int
    file_hash: str
    chunks:    List[ChunkInfo]

    @classmethod
    def from_dict(cls, path: str, d: dict) -> "FileEntry":
        return cls(
            path      = path,
            size      = d["size"],
            file_hash = d["hash"],
            chunks    = [
                ChunkInfo(c["id"], c["offset"], c["size"])
                for c in d.get("chunks", [])
            ],
        )

    def chunk_ids(self) -> List[str]:
        return [c.chunk_id for c in self.chunks]


class DepotManifest:
    def __init__(self, d: dict):
        self.app_id       = d.get("app_id", "")
        self.depot_id     = d.get("depot_id", 1)
        self.channel      = d.get("channel", "stable")
        self.build_number = d.get("build_number", 1)
        self.build_id     = d.get("build_id", "")
        self.created_at   = d.get("created_at", "")
        self.chunk_size   = d.get("chunk_size", 4 * 1024 * 1024)
        self.files: Dict[str, FileEntry] = {
            path: FileEntry.from_dict(path, info)
            for path, info in d.get("files", {}).items()
        }

    @classmethod
    def from_json(cls, text: str) -> "DepotManifest":
        return cls(json.loads(text))

    @property
    def total_size(self) -> int:
        return sum(e.size for e in self.files.values())

    def get_all_chunk_ids(self) -> Set[str]:
        ids = set()
        for e in self.files.values():
            ids.update(e.chunk_ids())
        return ids


# ── DepotClient ───────────────────────────────────────────────────────────────

class DepotClient:
    """
    Клиент для скачивания/обновления depot.
    
    Алгоритм update():
      1. Скачиваем depot_manifest.json
      2. Сравниваем с локальным состоянием (хэши файлов)
      3. Определяем нужные чанки
      4. Параллельно скачиваем чанки (max_workers потоков)
      5. Собираем файлы из чанков
      6. Верифицируем
      7. Сохраняем локальный манифест
    """

    def __init__(
        self,
        server_url:  str,
        username:    str,
        password:    str,
        remote_path: str,
        local_dir:   str,
        verify_ssl:  bool = True,
        max_workers: int  = 4,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
        on_log:      Optional[Callable[[str], None]] = None,
    ):
        self.base_url    = server_url.rstrip("/")
        self.remote_path = remote_path.strip("/")
        self.local_dir   = Path(local_dir)
        self.max_workers = max_workers
        self.on_progress = on_progress or (lambda *_: None)
        self.on_log      = on_log or print

        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(username, password)
        self.session.verify = verify_ssl
        self.session.headers["User-Agent"] = "Uploder-Launcher/2.0"

        # Локальный манифест (кэш предыдущего состояния)
        self._local_manifest_path = self.local_dir / ".depot" / "manifest.json"
        self._local_manifest: Optional[DepotManifest] = None

        self._stop_event = threading.Event()

    # ── URL helpers ───────────────────────────────────────────────────────────

    def _url(self, *parts: str) -> str:
        segs = [self.base_url, self.remote_path] + [p.strip("/") for p in parts if p]
        return "/".join(segs)

    def _chunk_url(self, chunk_id: str) -> str:
        return self._url(CHUNK_DIR, chunk_id[:2], chunk_id)

    # ── Low-level HTTP ────────────────────────────────────────────────────────

    def _get(self, url: str, timeout: int = 60) -> Optional[bytes]:
        try:
            r = self.session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.content
            self.on_log(f"⚠️ HTTP {r.status_code}: {url}")
            return None
        except Exception as e:
            self.on_log(f"❌ Ошибка GET {url}: {e}")
            return None

    # ── Manifest ──────────────────────────────────────────────────────────────

    def fetch_manifest(self) -> Optional[DepotManifest]:
        """Скачиваем depot_manifest.json с сервера"""
        url  = self._url(MANIFEST_FILE)
        data = self._get(url)
        if data is None:
            return None
        try:
            return DepotManifest.from_json(data.decode("utf-8"))
        except Exception as e:
            self.on_log(f"❌ Ошибка парсинга манифеста: {e}")
            return None

    def load_local_manifest(self) -> Optional[DepotManifest]:
        """Загружаем локальный кэш манифеста"""
        if not self._local_manifest_path.exists():
            return None
        try:
            return DepotManifest.from_json(
                self._local_manifest_path.read_text(encoding="utf-8")
            )
        except Exception:
            return None

    def save_local_manifest(self, manifest: DepotManifest):
        """Сохраняем манифест локально"""
        self._local_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self._local_manifest_path.write_text(
            json.dumps(manifest.files and {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Сохраняем сырой JSON для воспроизведения
        raw_path = self._local_manifest_path.parent / "manifest_raw.json"
        # В реальном коде — сохраняем скачанный bytes

    # ── File verification ─────────────────────────────────────────────────────

    def _file_needs_update(self, entry: FileEntry) -> bool:
        """Нужно ли обновлять файл? Быстрая проверка размера + хэш"""
        local_path = self.local_dir / entry.path
        if not local_path.exists():
            return True
        if local_path.stat().st_size != entry.size:
            return True
        # Хэш — медленно, делаем только если размер совпал
        actual_hash = self._hash_file(local_path)
        return actual_hash != entry.file_hash

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    # ── Chunk download ────────────────────────────────────────────────────────

    def _download_chunk(self, chunk_id: str) -> Optional[bytes]:
        """Скачиваем один чанк по chunk_id"""
        url  = self._chunk_url(chunk_id)
        data = self._get(url, timeout=120)
        if data is None:
            return None
        # Верифицируем integrity
        actual = hashlib.sha256(data).hexdigest()
        if actual != chunk_id:
            self.on_log(f"❌ Чанк повреждён: {chunk_id[:12]} (хэш не совпадает)")
            return None
        return data

    # ── File assembly ─────────────────────────────────────────────────────────

    def _assemble_file(
        self,
        entry: FileEntry,
        chunk_cache: Dict[str, bytes],
    ) -> bool:
        """Собираем файл из чанков"""
        out_path = self.local_dir / entry.path
        out_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with open(out_path, "wb") as f:
                for chunk in sorted(entry.chunks, key=lambda c: c.offset):
                    data = chunk_cache.get(chunk.chunk_id)
                    if data is None:
                        self.on_log(f"❌ Отсутствует чанк {chunk.chunk_id[:12]} для {entry.path}")
                        return False
                    f.write(data)
            return True
        except Exception as e:
            self.on_log(f"❌ Ошибка сборки {entry.path}: {e}")
            return False

    # ── Main update flow ──────────────────────────────────────────────────────

    def update(self, verify_after: bool = True, force: bool = False) -> bool:
        """
        Полный цикл обновления.
        
        verify_after=True — верифицировать собранные файлы после загрузки.
        force=True        — скачать всё заново даже если файлы актуальны.
        
        Возвращает True если всё успешно.
        """
        self.on_log("🚀 Запуск обновления...")

        # 1. Скачиваем манифест
        self.on_log("📥 Загружаем манифест...")
        manifest = self.fetch_manifest()
        if manifest is None:
            self.on_log("❌ Не удалось загрузить манифест")
            return False

        self.on_log(
            f"📋 Манифест: {manifest.app_id} build #{manifest.build_number} "
            f"({manifest.build_id}), файлов: {len(manifest.files)}"
        )

        # 2. Определяем файлы для обновления
        self.on_log("🔍 Проверяем локальные файлы...")
        files_to_update: List[FileEntry] = []

        if force:
            files_to_update = list(manifest.files.values())
        else:
            total = len(manifest.files)
            for i, (_, entry) in enumerate(manifest.files.items()):
                if self._stop_event.is_set():
                    return False
                self.on_progress(i + 1, total, f"Проверка: {entry.path}")
                if self._file_needs_update(entry):
                    files_to_update.append(entry)

        if not files_to_update:
            self.on_log("✅ Все файлы актуальны!")
            return True

        self.on_log(f"📦 Требует обновления: {len(files_to_update)} файлов")

        # 3. Собираем нужные chunk_id
        needed_chunks: Set[str] = set()
        chunk_to_file: Dict[str, List[FileEntry]] = {}

        for entry in files_to_update:
            for chunk in entry.chunks:
                needed_chunks.add(chunk.chunk_id)
                chunk_to_file.setdefault(chunk.chunk_id, []).append(entry)

        self.on_log(f"📦 Уникальных чанков к загрузке: {len(needed_chunks)}")

        # 4. Параллельная загрузка чанков
        chunk_cache: Dict[str, bytes] = {}
        failed_chunks: Set[str] = set()
        done = 0
        total_chunks = len(needed_chunks)

        self.on_log(f"⬇️ Загружаем {total_chunks} чанков ({self.max_workers} потоков)...")

        def _dl(cid: str) -> Tuple[str, Optional[bytes]]:
            return cid, self._download_chunk(cid)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(_dl, cid): cid for cid in needed_chunks}
            for future in as_completed(futures):
                if self._stop_event.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    return False

                cid, data = future.result()
                done += 1
                self.on_progress(done, total_chunks, f"Чанк {cid[:8]}…")

                if data is not None:
                    chunk_cache[cid] = data
                    self.on_log(f"  ✅ {cid[:12]} ({len(data) // 1024} KB)")
                else:
                    failed_chunks.add(cid)

        if failed_chunks:
            # Находим файлы с ошибками
            broken = set()
            for cid in failed_chunks:
                for entry in chunk_to_file.get(cid, []):
                    broken.add(entry.path)
            self.on_log(f"❌ Не удалось скачать {len(failed_chunks)} чанков, затронуто файлов: {len(broken)}")

        # 5. Сборка файлов
        self.on_log("🔧 Собираем файлы из чанков...")
        ok_files, fail_files = 0, 0

        for i, entry in enumerate(files_to_update):
            if self._stop_event.is_set():
                break
            self.on_progress(i + 1, len(files_to_update), f"Сборка: {entry.path}")

            # Пропускаем файлы с недостающими чанками
            missing = [c.chunk_id for c in entry.chunks if c.chunk_id not in chunk_cache]
            if missing:
                self.on_log(f"⚠️ Пропускаем {entry.path} — недостаёт {len(missing)} чанков")
                fail_files += 1
                continue

            if self._assemble_file(entry, chunk_cache):
                ok_files += 1
            else:
                fail_files += 1

        # 6. Верификация
        if verify_after and ok_files > 0:
            self.on_log("🔍 Верификация...")
            verify_ok, verify_fail = 0, 0
            for entry in files_to_update:
                local_path = self.local_dir / entry.path
                if not local_path.exists():
                    continue
                actual = self._hash_file(local_path)
                if actual == entry.file_hash:
                    verify_ok += 1
                else:
                    verify_fail += 1
                    self.on_log(f"❌ Верификация провалена: {entry.path}")
            self.on_log(f"✅ Верификация: {verify_ok} OK, {verify_fail} ошибок")

        # 7. Удаляем лишние файлы (которых нет в манифесте)
        self._cleanup_extra_files(manifest)

        result = fail_files == 0
        self.on_log(
            f"{'✅' if result else '⚠️'} Обновление завершено. "
            f"Успешно: {ok_files}, ошибок: {fail_files}"
        )
        return result

    def _cleanup_extra_files(self, manifest: DepotManifest):
        """Удаляем локальные файлы, которых нет в манифесте"""
        manifest_paths = set(manifest.files.keys())
        removed = 0
        for f in self.local_dir.rglob("*"):
            if f.is_file() and ".depot" not in str(f):
                rel = str(f.relative_to(self.local_dir)).replace("\\", "/")
                if rel not in manifest_paths:
                    f.unlink()
                    removed += 1
                    self.on_log(f"🗑 Удалён лишний файл: {rel}")
        if removed:
            self.on_log(f"🗑 Удалено лишних файлов: {removed}")

    def stop(self):
        self._stop_event.set()

    def close(self):
        self.session.close()


# ── CLI точка входа ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Depot Launcher Client")
    parser.add_argument("--server",      required=True)
    parser.add_argument("--user",        required=True)
    parser.add_argument("--password",    required=True)
    parser.add_argument("--remote-path", required=True)
    parser.add_argument("--local-dir",   required=True)
    parser.add_argument("--workers",     type=int, default=4)
    parser.add_argument("--force",       action="store_true")
    parser.add_argument("--no-verify",   action="store_true")
    parser.add_argument("--no-ssl",      action="store_true")
    args = parser.parse_args()

    client = DepotClient(
        server_url   = args.server,
        username     = args.user,
        password     = args.password,
        remote_path  = args.remote_path,
        local_dir    = args.local_dir,
        verify_ssl   = not args.no_ssl,
        max_workers  = args.workers,
    )

    ok = client.update(
        verify_after = not args.no_verify,
        force        = args.force,
    )
    client.close()
    sys.exit(0 if ok else 1)
