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
    байты чанков, в порядке добавления. Порядок задаёт вызывающий код
    (DepotSyncManager._pack_upload_order) — ПО ФАЙЛАМ манифеста, чанки
    одного файла подряд, в том же порядке их читает лаунчер
    (TESL/core/chunk_installer.py::_plan_chunk_order_packed), что даёт
    последовательное чтение pack-файлов на HDD сервера.
    До 2026-10-05 чанки добавлялись в sorted(chunk_id): pack получался
    срезом хэш-пространства, соседние чанки одного файла оказывались в
    разных pack, а лаунчер (с 2026-09-23 читает по файлам, не по хэшу —
    фикс от OOM) делал ПОЛНОСТЬЮ случайное чтение. Живой ladder-замер
    на реальном сервере (134ГБ/1455 pack, 2026-10-05) подтвердил
    физику напрямую: последовательные Range-GET — 37-64 МБ/с на
    1-24 потоках; случайные — 0.02-0.8 МБ/с, причём с РОСТОМ числа
    потоков хуже, не лучше (24 потока → 77% запросов ReadTimeout) —
    конкурентные случайные seek на HDD сервера интерферируют друг с
    другом. Это и было измеренной причиной "500МБ за 4 часа" на
    реальной установке.
  chunk_index.db (SQLite) — chunk_locations(chunk_id TEXT PRIMARY KEY,
    pack TEXT, offset INTEGER, size INTEGER) — единственный источник
    истины "где физически лежит этот chunk_id" для читающей стороны
    (TESL-Panel Range-GET, будущий launcher-side reader).
"""
try:
    import sqlite3
except ImportError:
    # Живой инцидент 2026-09-29, найден при запуске сквозного
    # интеграционного теста (см. TESL-Panel::tests/integration/) прямо
    # на сервере панели — тот же класс проблемы, что уже чинился в
    # TESL-Panel::builds_db.py: Python на этом сервере собран из
    # исходников без `_sqlite3` (C-расширение, нужен `libsqlite3-dev` в
    # системе НА МОМЕНТ сборки интерпретатора). Обычно TESL-Manager
    # запускается оператором на Windows, где `sqlite3` в stdlib есть
    # всегда — этот путь актуален только когда его код (как здесь,
    # через подпроцесс интеграционного теста) исполняется на ТАКОМ
    # Linux-сервере. Тот же fallback: `pysqlite3-binary` — самодостаточное
    # wheel со своим статически слинкованным libsqlite3, не зависит от
    # того, с чем был собран системный Python.
    import pysqlite3 as sqlite3
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# Живой инцидент (2026-09-24): исходный DEFAULT_PACK_SIZE=256MB и
# HDD_PACK_SIZE=1GB (первая версия этого файла) реально сломали
# публикацию через TESL-Panel на проде — "Не удалось загрузить
# pack-00001.bin", после того как этот же класс ошибки перестал
# молча глотаться (см. panel_client.py::PanelHTTP.last_error), реальная
# причина оказалась 413 от nginx: TESL-Panel's `client_max_body_size
# 64m` был рассчитан на одиночные чанки (максимум 4MB), никто не поднял
# его, когда появились pack-файлы. Отдельно от этого — сервер за
# Cloudflare (Proxied), а у Cloudflare Free/Pro-планов есть СВОЙ,
# неизменяемый лимит на тело запроса ~100MB — эту границу нельзя
# обойти никакими настройками nginx/origin вообще, она физическая
# для этого транспорта (см. TESL-Panel/infra/nginx-tesl-panel.conf.
# template за исправленный client_max_body_size — тот фикс закрывает
# ТОЛЬКО nginx-часть, а не Cloudflare-часть). Оба размера ниже
# уменьшены так, чтобы гарантированно проходить под 100MB с запасом,
# независимо от тарифа Cloudflare — выигрыш от больших pack-файлов
# на HDD (меньше файловых метаданных на ФС) всё ещё есть при 96MB
# против 64MB, просто гораздо скромнее, чем изначальные 256MB/1GB,
# которые никогда реально не проходили бы через этот транспорт.
DEFAULT_PACK_SIZE = 64 * 1024 * 1024    # 64MB — SSD, тот же лимит, что уже был у nginx на чанки
HDD_PACK_SIZE     = 96 * 1024 * 1024    # 96MB — HDD, крупнее SSD, но всё ещё с запасом под 100MB


class PackWriter:
    """Пишет чанки ЛОКАЛЬНО (во временную/выходную директорию) — саму
    загрузку на сервер делает вызывающий код (DepotSyncManager), эта
    прослойка не знает про сеть вообще, как и chunk_manager.py."""

    def __init__(
        self,
        out_dir: Path,
        pack_size: int = DEFAULT_PACK_SIZE,
        start_index: int = 0,
        on_pack_complete: Optional[Callable[[Path], None]] = None,
    ):
        """
        `on_pack_complete` — живой инцидент 2026-09-29: без него ВСЕ
        pack-файлы целой публикации пишутся на локальный диск (обычно
        `tempfile.mkdtemp()`, то есть системный TEMP — часто МЕНЬШИЙ по
        объёму диск, чем тот, где реально лежат исходники) и только
        ПОСЛЕ ТОГО, как упаковка ПОЛНОСТЬЮ закончится, начинается
        заливка (см. execute_sync_packed()) — то есть для крупной сборки
        (реальный случай: 174977 файлов, 157091 новых чанков) локально
        должна поместиться ВСЯ сборка целиком, прежде чем на сервер уйдёт
        хоть один байт. Живой инцидент: "[Errno 28] No space left on
        device" при упаковке ~171GB-сборки — диск, на котором лежит
        системный TEMP, оказался меньше суммарного объёма сборки.
        Callback вызывается с уже ЗАКРЫТЫМ файлом каждого только что
        завершённого pack'а (и с финальным — из finalize()) —
        вызывающий код (execute_sync_packed()) грузит его на сервер и
        удаляет локально немедленно, так что на диске одновременно лежит
        не вся сборка, а максимум несколько pack-файлов (столько, сколько
        в моменте не успело улететь на сервер) — независимо от того,
        сколько всего чанков в публикации. `None` (по умолчанию) —
        поведение НЕ меняется вообще: все pack-файлы остаются на диске
        до explicit finalize(), как раньше (обратная совместимость для
        существующих тестов/CLI, которым это не нужно).

        `start_index` — живой инцидент 2026-09-29: без него КАЖДЫЙ вызов
        начинает нумерацию заново с `pack-00001.bin`, независимо от того,
        сколько pack-файлов уже реально лежит на сервере с прошлых
        публикаций ЭТОЙ сборки. Второй (и любой следующий) публикация
        поверх уже существующего chunk_index.db тогда СОВПАДАЕТ именами
        со старыми pack-файлами — `storage.put_bytes()` на TESL-Panel
        просто заменяет файл по пути, никакой защиты от коллизии имён
        нет — и старые записи `chunk_locations`, всё ещё указывающие на
        `pack-00001.bin` по СТАРЫМ offset/size, начинают читать байты из
        СОВЕРШЕННО ДРУГОГО (перезаписанного) файла: sha256 не совпадёт
        (данные другие) либо Range выйдет за пределы нового файла (416,
        если он оказался короче старого). Живой прогон это подтвердил
        буквально: 1846 `sha256_mismatch` + 174 `http_416` на 2020 отказов
        из 2120 нужных чанков — почти все чанки, упакованные не в
        последнюю публикацию. См. `execute_sync_packed()` — вызывающий
        код обязан передать сюда максимальный номер уже занятого pack-а
        (из уже смёрженного `existing_index`), не ноль.
        """
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.pack_size = pack_size
        self._pack_index = start_index
        self._cur_file = None
        self._cur_path: Path = None
        self._cur_offset = 0
        self.on_pack_complete = on_pack_complete
        # chunk_id -> (pack_filename, offset, size) — только чанки,
        # добавленные ЭТИМ вызовом PackWriter (новые для этой публикации);
        # слияние со старым индексом с прошлых публикаций — забота
        # вызывающего кода (execute_sync_packed), не этого класса.
        self.locations: Dict[str, Tuple[str, int, int]] = {}
        self.pack_paths: List[Path] = []

    def _open_new_pack(self) -> None:
        prev_path = self._cur_path
        self._close_current_file()
        # Предыдущий pack (если был) только что закрыт и больше не
        # пишется — самое время его выгрузить/удалить, если вызывающий
        # код это делает (см. on_pack_complete в докстринге __init__).
        if prev_path is not None and self.on_pack_complete is not None:
            self.on_pack_complete(prev_path)
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
        """Закрывает и (если задан on_pack_complete) сдаёт ПОСЛЕДНИЙ
        ещё не сданный pack — все предыдущие уже прошли через
        _open_new_pack() выше, этот единственный не имел "следующего"
        пака, который бы его закрыл раньше."""
        last_path = self._cur_path
        self._close_current_file()
        if last_path is not None and self.on_pack_complete is not None:
            self.on_pack_complete(last_path)


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
