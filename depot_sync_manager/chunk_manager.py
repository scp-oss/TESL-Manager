# ==================== chunk_manager.py ====================
"""
Chunk Manager — ядро depot-системы.
Нарезает файлы на блоки, считает хэши, обеспечивает content-addressed storage.
Совместим с Nextcloud WebDAV (remote.php/dav).
"""
import os
import hashlib
import json
from pathlib import Path
from typing import Iterator, List, Dict, Optional, Tuple
from dataclasses import dataclass, asdict
from PyQt6.QtCore import QObject, pyqtSignal


# ── Константы ─────────────────────────────────────────────────────────────────

DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024   # 4 MB — баланс между delta-точностью и overhead
MIN_CHUNK_SIZE     = 512 * 1024         # 512 KB
MAX_CHUNK_SIZE     = 64 * 1024 * 1024  # 64 MB

CHUNKS_DIR  = "chunks"          # подпапка внутри remote_path
VERSIONS_DIR = "versions"       # история манифестов
DEPOT_META   = "depot.json"     # метаданные депо (каналы, последняя версия)


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class ChunkInfo:
    """Информация об одном чанке файла"""
    chunk_id: str       # SHA-256 содержимого (hex)
    offset: int         # байтовое смещение в исходном файле
    size: int           # фактический размер чанка (последний может быть меньше)
    
    def to_dict(self) -> dict:
        return {
            "id":     self.chunk_id,
            "offset": self.offset,
            "size":   self.size,
        }
    
    @classmethod
    def from_dict(cls, d: dict) -> "ChunkInfo":
        return cls(
            chunk_id = d["id"],
            offset   = d["offset"],
            size     = d["size"],
        )


@dataclass
class FileEntry:
    """Запись о файле в depot-манифесте"""
    path: str           # относительный путь (всегда forward-slash)
    size: int
    file_hash: str      # SHA-256 всего файла (hex, без префикса)
    chunks: List[ChunkInfo]
    
    def to_dict(self) -> dict:
        return {
            "size":   self.size,
            "hash":   self.file_hash,
            "chunks": [c.to_dict() for c in self.chunks],
        }
    
    @classmethod
    def from_dict(cls, path: str, d: dict) -> "FileEntry":
        return cls(
            path      = path,
            size      = d["size"],
            file_hash = d["hash"],
            chunks    = [ChunkInfo.from_dict(c) for c in d.get("chunks", [])],
        )
    
    def chunk_ids(self) -> List[str]:
        return [c.chunk_id for c in self.chunks]


# ── ChunkManager ──────────────────────────────────────────────────────────────

class ChunkManager(QObject):
    """
    Ядро depot-системы.
    
    Отвечает за:
      • нарезку файлов на чанки
      • вычисление хэшей (файл + каждый чанк)
      • сравнение с предыдущим манифестом для delta
      • формирование depot-манифеста нового формата
      • построение плана загрузки (какие чанки новые, какие уже есть)
    
    НЕ отвечает за сетевые операции — ими занимается DepotSyncManager.
    """

    progress = pyqtSignal(int, int, str)   # current, total, message
    log      = pyqtSignal(str)

    def __init__(self, chunk_size: int = DEFAULT_CHUNK_SIZE):
        super().__init__()
        self.chunk_size = max(MIN_CHUNK_SIZE, min(MAX_CHUNK_SIZE, chunk_size))

    # ── Нарезка одного файла ─────────────────────────────────────────────────

    def slice_file(self, file_path: Path) -> Tuple[str, List[ChunkInfo]]:
        """
        Нарезать файл на чанки.
        Возвращает (file_sha256_hex, [ChunkInfo]).
        Читает файл ровно один раз — одновременно считает хэш файла и хэши чанков.
        """
        file_hasher = hashlib.sha256()
        chunks: List[ChunkInfo] = []
        offset = 0

        with open(file_path, "rb") as f:
            while True:
                data = f.read(self.chunk_size)
                if not data:
                    break

                # Хэш файла — обновляем непрерывно
                file_hasher.update(data)

                # Хэш чанка — отдельный объект
                chunk_hash = hashlib.sha256(data).hexdigest()
                chunks.append(ChunkInfo(
                    chunk_id = chunk_hash,
                    offset   = offset,
                    size     = len(data),
                ))
                offset += len(data)

        return file_hasher.hexdigest(), chunks

    def slice_file_entry(self, file_path: Path, rel_path: str) -> FileEntry:
        """Обёртка: нарезать файл и вернуть FileEntry"""
        size = file_path.stat().st_size
        if size == 0:
            # Пустой файл — специальный случай
            empty_hash = hashlib.sha256(b"").hexdigest()
            return FileEntry(
                path      = rel_path,
                size      = 0,
                file_hash = empty_hash,
                chunks    = [],
            )
        file_hash, chunks = self.slice_file(file_path)
        return FileEntry(
            path      = rel_path,
            size      = size,
            file_hash = file_hash,
            chunks    = chunks,
        )

    # ── Chunk path на сервере ────────────────────────────────────────────────

    @staticmethod
    def chunk_remote_path(chunk_id: str) -> str:
        """
        Content-addressed путь чанка внутри chunks/.
        Первые 2 символа хэша — подпапка (как в Git objects).
        chunks/a1/a1b2c3d4...  (избегаем тысяч файлов в одной папке)
        """
        return f"{CHUNKS_DIR}/{chunk_id[:2]}/{chunk_id}"

    # ── Delta: сравнение манифестов ──────────────────────────────────────────

    def compute_delta(
        self,
        new_entries:  Dict[str, FileEntry],
        prev_entries: Dict[str, FileEntry],
        remote_chunk_ids: set,          # уже загруженные chunk_id на сервере
    ) -> "DepotDelta":
        """
        Сравнивает новый и предыдущий манифест.
        Возвращает DepotDelta с планом что загружать / удалять.
        """
        delta = DepotDelta()

        new_paths  = set(new_entries.keys())
        prev_paths = set(prev_entries.keys())

        # Файлы для удаления с сервера
        delta.files_to_delete = list(prev_paths - new_paths)

        for path, new_entry in new_entries.items():
            prev_entry = prev_entries.get(path)

            if prev_entry is None:
                # Новый файл — нужны все его чанки
                delta.files_new.append(path)
                for chunk in new_entry.chunks:
                    if chunk.chunk_id not in remote_chunk_ids:
                        delta.chunks_to_upload.add(chunk.chunk_id)
            elif prev_entry.file_hash != new_entry.file_hash:
                # Файл изменился — нужны только новые чанки
                delta.files_changed.append(path)
                prev_chunk_ids = set(prev_entry.chunk_ids())
                for chunk in new_entry.chunks:
                    if chunk.chunk_id not in prev_chunk_ids and \
                       chunk.chunk_id not in remote_chunk_ids:
                        delta.chunks_to_upload.add(chunk.chunk_id)
            else:
                # Файл не изменился
                delta.files_unchanged.append(path)

        return delta

    # ── Сканирование директории ──────────────────────────────────────────────

    def scan_directory(
        self,
        local_dir:  Path,
        excludes:   List[str],
        prev_manifest: Optional["DepotManifest"] = None,
    ) -> Tuple[Dict[str, FileEntry], "ScanStats"]:
        """
        Сканирует локальную директорию, нарезает файлы на чанки.
        Если передан prev_manifest — пропускает неизменившиеся файлы
        (оптимизация: переиспользуем старые ChunkInfo если size+mtime совпадают).
        
        Возвращает (entries_dict, stats).
        """
        from manifest_manager import ManifestManager  # lazy import
        mm = ManifestManager.__new__(ManifestManager)
        mm.config = {}

        # Собираем список файлов
        all_files: List[Path] = []
        for root, dirs, files in os.walk(local_dir):
            dirs[:] = [
                d for d in dirs
                if not mm.should_exclude(Path(root) / d, excludes)
            ]
            for f in files:
                fp = Path(root) / f
                if not mm.should_exclude(fp, excludes):
                    all_files.append(fp)

        total = len(all_files)
        stats = ScanStats(total_files=total)
        entries: Dict[str, FileEntry] = {}

        prev_files = (prev_manifest.files if prev_manifest else {})

        for i, file_path in enumerate(all_files):
            rel_path = str(file_path.relative_to(local_dir)).replace("\\", "/")
            self.progress.emit(i + 1, total, f"Хэшируем: {rel_path}")

            try:
                stat = file_path.stat()

                # Оптимизация: если файл не изменился — берём чанки из предыдущего манифеста
                if rel_path in prev_files:
                    prev_entry = prev_files[rel_path]
                    # Сравниваем размер — быстрая проверка без чтения файла
                    if prev_entry.size == stat.st_size:
                        # Перепроверяем полный хэш только если размер совпал
                        actual_hash, chunks = self.slice_file(file_path)
                        if actual_hash == prev_entry.file_hash:
                            entries[rel_path] = FileEntry(
                                path=rel_path,
                                size=prev_entry.size,
                                file_hash=prev_entry.file_hash,
                                chunks=prev_entry.chunks,
                            )
                            stats.unchanged += 1
                            continue

                # Новый или изменившийся файл — нарезаем
                entry = self.slice_file_entry(file_path, rel_path)
                entries[rel_path] = entry
                stats.processed += 1

            except PermissionError:
                self.log.emit(f"⚠️ Нет доступа: {rel_path}")
                stats.errors += 1
            except Exception as e:
                self.log.emit(f"⚠️ Ошибка обработки {rel_path}: {e}")
                stats.errors += 1

        return entries, stats

    # ── Сканирование НЕСКОЛЬКИХ компонентных папок в один манифест ───────────
    # (Skyrim / MO2p / MO2ext — см. DepotTab в depot_tab.py и
    # CLAUDE.md "Компонентная модель для Депо (chunks)"). Каждый компонент —
    # свой корень на диске, свои excludes; в итоговом манифесте пути
    # получают префикс "<Компонент>/", чтобы три дерева не пересекались и
    # чтобы при появлении читающей стороны (будущий launcher-reader) было
    # однозначно видно, из какого компонента взят файл.

    def scan_components(
        self,
        components: Dict[str, dict],   # {name: {local_dir, included, excludes}}
        prev_manifest: Optional["DepotManifest"] = None,
    ) -> Tuple[Dict[str, FileEntry], "ScanStats", Dict[str, str]]:
        """Возвращает (entries, суммарная ScanStats, {component: local_dir}
        по факту включённых и заданных компонентов)."""
        merged_entries: Dict[str, FileEntry] = {}
        total_stats = ScanStats()
        component_roots: Dict[str, str] = {}

        prev_files = prev_manifest.files if prev_manifest else {}

        for name, comp_cfg in components.items():
            if not comp_cfg.get("included"):
                continue
            local_dir = (comp_cfg.get("local_dir") or "").strip()
            if not local_dir:
                continue
            component_roots[name] = local_dir

            prefix = f"{name}/"
            comp_prev = {
                p[len(prefix):]: e
                for p, e in prev_files.items()
                if p.startswith(prefix)
            }
            comp_prev_manifest = _PrevFilesSlice(comp_prev) if comp_prev else None

            self.log.emit(f"📁 Компонент «{name}»: {local_dir}")
            entries, stats = self.scan_directory(
                local_dir     = Path(local_dir),
                excludes      = comp_cfg.get("excludes", []),
                prev_manifest = comp_prev_manifest,
            )

            for rel_path, entry in entries.items():
                full_path = f"{name}/{rel_path}"
                merged_entries[full_path] = FileEntry(
                    path      = full_path,
                    size      = entry.size,
                    file_hash = entry.file_hash,
                    chunks    = entry.chunks,
                )

            total_stats.total_files += stats.total_files
            total_stats.processed   += stats.processed
            total_stats.unchanged   += stats.unchanged
            total_stats.errors      += stats.errors

        return merged_entries, total_stats, component_roots

    # ── Верификация файла на клиенте ─────────────────────────────────────────

    def verify_file(self, file_path: Path, entry: FileEntry) -> Tuple[bool, str]:
        """
        Верификация локального файла по depot-манифесту.
        Сначала быстрая проверка размера, потом хэш.
        """
        if not file_path.exists():
            return False, "файл отсутствует"

        try:
            actual_size = file_path.stat().st_size
            if actual_size != entry.size:
                return False, f"размер {actual_size} ≠ {entry.size}"

            file_hash, _ = self.slice_file(file_path)
            if file_hash != entry.file_hash:
                return False, f"хэш не совпадает"

            return True, "ok"
        except Exception as e:
            return False, str(e)

    # ── Сборка файла из чанков (для лаунчера) ───────────────────────────────

    @staticmethod
    def assemble_file(
        chunk_data: Dict[str, bytes],   # chunk_id → bytes
        entry: FileEntry,
        output_path: Path,
    ) -> bool:
        """
        Собирает файл из чанков в правильном порядке.
        chunk_data — словарь {chunk_id: bytes} для всех чанков файла.
        """
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as f:
                for chunk_info in sorted(entry.chunks, key=lambda c: c.offset):
                    data = chunk_data.get(chunk_info.chunk_id)
                    if data is None:
                        raise ValueError(f"Отсутствует чанк: {chunk_info.chunk_id}")
                    f.write(data)
            return True
        except Exception:
            return False


# ── Delta plan ────────────────────────────────────────────────────────────────

class DepotDelta:
    """Результат сравнения двух манифестов — план изменений"""

    def __init__(self):
        self.files_new:       List[str] = []
        self.files_changed:   List[str] = []
        self.files_unchanged: List[str] = []
        self.files_to_delete: List[str] = []
        self.chunks_to_upload: set = set()  # chunk_id которые нужно залить

    @property
    def total_new_files(self) -> int:
        return len(self.files_new) + len(self.files_changed)

    @property
    def is_empty(self) -> bool:
        return (
            not self.files_new and
            not self.files_changed and
            not self.files_to_delete
        )

    def summary(self) -> str:
        parts = []
        if self.files_new:
            parts.append(f"новых: {len(self.files_new)}")
        if self.files_changed:
            parts.append(f"изменено: {len(self.files_changed)}")
        if self.files_to_delete:
            parts.append(f"удалить: {len(self.files_to_delete)}")
        if self.files_unchanged:
            parts.append(f"без изменений: {len(self.files_unchanged)}")
        if self.chunks_to_upload:
            parts.append(f"чанков к загрузке: {len(self.chunks_to_upload)}")
        return ", ".join(parts) if parts else "нет изменений"


# ── ScanStats ─────────────────────────────────────────────────────────────────

class ScanStats:
    def __init__(self, total_files: int = 0):
        self.total_files = total_files
        self.processed   = 0   # нарезаны заново
        self.unchanged   = 0   # взяты из предыдущего манифеста
        self.errors      = 0

    def __str__(self):
        return (
            f"Всего: {self.total_files}, "
            f"обработано: {self.processed}, "
            f"без изменений: {self.unchanged}, "
            f"ошибок: {self.errors}"
        )


class _PrevFilesSlice:
    """Лёгкая обёртка {files: dict} — scan_directory() читает только
    prev_manifest.files, полноценный DepotManifest тут не нужен. Используется
    scan_components() чтобы передать в scan_directory() только те записи
    предыдущего манифеста, что относятся к ОДНОМУ компоненту (с уже
    снятым префиксом "<Компонент>/")."""
    def __init__(self, files: Dict[str, FileEntry]):
        self.files = files


# ── DepotManifest ─────────────────────────────────────────────────────────────

class DepotManifest:
    """
    Depot-манифест нового формата.
    Совместим с launcher-клиентом.
    
    Структура на сервере:
      <remote_path>/
        depot.json              ← метаданные депо (каналы)
        depot_manifest.json     ← текущий актуальный манифест
        versions/
          <build_id>.json       ← история
        chunks/
          <xx>/<chunk_id>       ← content-addressed блоки
    """

    VERSION = "2"               # версия формата манифеста

    def __init__(
        self,
        app_id:       str = "",
        depot_id:     int = 1,
        channel:      str = "stable",
        chunk_size:   int = DEFAULT_CHUNK_SIZE,
        build_number: int = 1,
    ):
        import time, uuid
        self.format_version = self.VERSION
        self.app_id         = app_id
        self.depot_id       = depot_id
        self.channel        = channel      # stable | beta | dev
        self.chunk_size     = chunk_size
        self.build_number   = build_number
        self.build_id       = uuid.uuid4().hex[:12]
        self.created_at     = ""           # заполняется при to_dict()
        self.files:          Dict[str, FileEntry] = {}
        self.excludes:       List[str] = []
        self.local_dir:      str = ""

    # ── Сериализация ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        from datetime import datetime
        self.created_at = datetime.now().isoformat()
        return {
            "format_version": self.format_version,
            "app_id":         self.app_id,
            "depot_id":       self.depot_id,
            "channel":        self.channel,
            "chunk_size":     self.chunk_size,
            "build_number":   self.build_number,
            "build_id":       self.build_id,
            "created_at":     self.created_at,
            "local_dir":      self.local_dir,
            "excludes":       self.excludes,
            "stats": {
                "file_count":  len(self.files),
                "total_size":  sum(e.size for e in self.files.values()),
                "total_chunks": sum(len(e.chunks) for e in self.files.values()),
            },
            "files": {
                path: entry.to_dict()
                for path, entry in self.files.items()
            },
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> "DepotManifest":
        m = cls(
            app_id       = d.get("app_id", ""),
            depot_id     = d.get("depot_id", 1),
            channel      = d.get("channel", "stable"),
            chunk_size   = d.get("chunk_size", DEFAULT_CHUNK_SIZE),
            build_number = d.get("build_number", 1),
        )
        m.format_version = d.get("format_version", "2")
        m.build_id       = d.get("build_id", "")
        m.created_at     = d.get("created_at", "")
        m.local_dir      = d.get("local_dir", "")
        m.excludes       = d.get("excludes", [])
        m.files = {
            path: FileEntry.from_dict(path, info)
            for path, info in d.get("files", {}).items()
        }
        return m

    @classmethod
    def from_json(cls, text: str) -> "DepotManifest":
        return cls.from_dict(json.loads(text))

    @classmethod
    def from_file(cls, path: Path) -> Optional["DepotManifest"]:
        try:
            return cls.from_json(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    # ── Утилиты ───────────────────────────────────────────────────────────────

    def get_all_chunk_ids(self) -> set:
        """Все chunk_id, упомянутые в манифесте"""
        ids = set()
        for entry in self.files.values():
            ids.update(entry.chunk_ids())
        return ids

    @property
    def total_size(self) -> int:
        return sum(e.size for e in self.files.values())

    @property
    def total_chunks(self) -> int:
        return sum(len(e.chunks) for e in self.files.values())

    def human_size(self) -> str:
        s = self.total_size
        for unit in ("B", "KB", "MB", "GB"):
            if s < 1024:
                return f"{s:.1f} {unit}"
            s /= 1024
        return f"{s:.1f} TB"

    def version_label(self) -> str:
        return f"build #{self.build_number} ({self.build_id})"
