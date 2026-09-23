# ==================== pack_writer.py ====================
"""
Упаковка чанков в крупные pack-файлы с индексом (chunk_id -> pack, offset,
size) — вместо одного физического файла на чанк (chunks/<xx>/<id>). Тот
же принцип, что git packfiles / Steam-подобные депо — см. TESL/CLAUDE.md
"Живая диагностика скорости установки", пункт 5 ("упаковка чанков в
несколько крупных pack-файлов с индексом... filesystem-агностичный
способ добиться реальной последовательности"). Реальная сборка на
WebDAV — 155290 уникальных чанков = 155290 отдельных файлов в chunks/ —
это и порождает случайное чтение на диске сервера (см. тот же раздел,
пункт 3, iostat-подтверждение).

НЕ заменяет старый chunks/<xx>/<id> протокол — новый, опциональный,
включается явно на стороне DepotSyncManager (см. execute_sync_packed()),
существующая продакшен-сборка на WebDAV в старом формате продолжает
работать без единого изменения.

Формат:
  packs/pack-00001.bin, pack-00002.bin, ...  — сырые конкатенированные
    байты чанков, в порядке добавления (см. ниже — вызывающий код
    добавляет их в sorted(chunk_id) порядке, том же, в каком лаунчер и
    так их запрашивает, см. TESL/core/chunk_installer.py — совпадение
    порядка упаковки с порядком чтения и есть весь смысл этой схемы).
  chunk_index.db (SQLite) — chunk_locations(chunk_id TEXT PRIMARY KEY,
    pack TEXT, offset INTEGER, size INTEGER) — единственный источник
    истины "где физически лежит этот chunk_id" для читающей стороны
    (TESL-Panel Range-GET, будущий launcher-side reader).
"""
import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple

DEFAULT_PACK_SIZE = 256 * 1024 * 1024   # "кластер" — до скольки байт в одном pack-файле


class PackWriter:
    """Пишет чанки ЛОКАЛЬНО (во временную/выходную директорию) — саму
    загрузку на сервер делает вызывающий код (DepotSyncManager), эта
    прослойка не знает про сеть вообще, как и chunk_manager.py."""

    def __init__(self, out_dir: Path, pack_size: int = DEFAULT_PACK_SIZE):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.pack_size = pack_size
        self._pack_index = 0
        self._cur_file = None
        self._cur_path: Path = None
        self._cur_offset = 0
        # chunk_id -> (pack_filename, offset, size) — только чанки,
        # добавленные ЭТИМ вызовом PackWriter (новые для этой публикации);
        # слияние со старым индексом с прошлых публикаций — забота
        # вызывающего кода (execute_sync_packed), не этого класса.
        self.locations: Dict[str, Tuple[str, int, int]] = {}
        self.pack_paths: List[Path] = []

    def _open_new_pack(self) -> None:
        self._close_current_file()
        self._pack_index += 1
        name = f"pack-{self._pack_index:05d}.bin"
        self._cur_path = self.out_dir / name
        self._cur_file = open(self._cur_path, "wb")
        self._cur_offset = 0
        self.pack_paths.append(self._cur_path)

    def _close_current_file(self) -> None:
        if self._cur_file is not None:
            self._cur_file.close()
            self._cur_file = None

    def add_chunk(self, chunk_id: str, data: bytes) -> None:
        """Идемпотентно — повторный add_chunk с уже упакованным chunk_id
        молча игнорируется (тот же чанк может встретиться дважды при
        дедупе между файлами, вызывающий код и так использует set
        chunks_to_upload, но лишняя защита здесь дёшева и не вредит)."""
        if chunk_id in self.locations:
            return
        if self._cur_file is None or self._cur_offset + len(data) > self.pack_size:
            self._open_new_pack()
        self._cur_file.write(data)
        self.locations[chunk_id] = (self._cur_path.name, self._cur_offset, len(data))
        self._cur_offset += len(data)

    def finalize(self) -> None:
        self._close_current_file()


def write_chunk_index_db(
    db_path: Path,
    locations: Dict[str, Tuple[str, int, int]],
) -> None:
    """Пишет ПОЛНЫЙ (не инкрементальный) chunk_index.db с нуля из уже
    смёрженного словаря (старые записи с прошлых публикаций + новые из
    этого PackWriter — слияние делает вызывающий код). Перезаписывает
    файл целиком, если он уже существовал — тот же принцип, что и
    depot.json/versions/<key>.json ниже в depot_sync_manager.py, простая
    полная перезапись небольшого метафайла проще и надёжнее
    инкрементального UPDATE по сети."""
    db_path = Path(db_path)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE chunk_locations "
            "(chunk_id TEXT PRIMARY KEY, pack TEXT NOT NULL, "
            "offset INTEGER NOT NULL, size INTEGER NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO chunk_locations (chunk_id, pack, offset, size) VALUES (?, ?, ?, ?)",
            [(cid, pack, off, size) for cid, (pack, off, size) in locations.items()],
        )
        conn.commit()
    finally:
        conn.close()


def read_chunk_index_db(db_path: Path) -> Dict[str, Tuple[str, int, int]]:
    """Обратное к write_chunk_index_db — используется и здесь (слияние
    с прошлой публикацией), и на стороне читателя (launcher, панель)."""
    db_path = Path(db_path)
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            row[0]: (row[1], row[2], row[3])
            for row in conn.execute("SELECT chunk_id, pack, offset, size FROM chunk_locations")
        }
    finally:
        conn.close()
