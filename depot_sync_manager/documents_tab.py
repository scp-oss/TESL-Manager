# ==================== documents_tab.py ====================
"""
Страница "📄 Документы и патчи" — прямой запрос пользователя (2026-09-24):
шаблоны настроек игры (документы) и .bat-патчи (применяются лаунчером
по порядку при обновлении) как ДВЕ дополнительные, общие на всю сборку
папки внутри неё (`documents/`, `patch/`) — решение из явно заданного
вопроса: "общие на сборку" (не отдельно под каждый компонент), потому
что так проще, а шаблоны/патчи в этом проекте не привязаны жёстко к
конкретному компоненту.

Работает ПОВЕРХ уже существующего, ничем не изменённого упакованного
депо (`chunks/`/`packs/`/`versions/`/`depot.json`/`depot_manifest.json`
/`chunk_index.db`) — эти две папки живут рядом с ним как обычные
маленькие файлы через тот же самый generic PUT/GET/DELETE/`files`
API TESL-Panel, что уже используют `depot_files_tab.py`/`_upload_file()`
(проверено отдельно: серверный `storage.list_files()` — рекурсивный
`rglob`, ему всё равно, что лежит в дереве). Никаких изменений на
стороне TESL-Panel для этого не потребовалось.

**Лаунчер (`scp-oss/TESL`) этим заходом НЕ трогается** — стоящая
инструкция пользователя с самого начала этого движка ("TESL (лаунчер)
— пока не трогаем его"). Эта страница готовит и публикует контракт
(раскладку `documents/`+`patch/`+`extras_manifest.json`), который
лаунчер должен будет читать и применять, когда до него дойдёт очередь
— сама логика "применить .ini-шаблон"/"прогнать .bat по порядку" на
клиенте игрока здесь не реализуется.

## Хеши и `extras_manifest.json`

Прямой запрос: "при изменении должна переписаться бд сборки" — раз
`documents/`+`patch/` не проходят через chunk-движок (не дедуплицируются,
не версионируются per-chunk, как основной депо), для них ведётся
отдельный маленький манифест на корне сборки — `extras_manifest.json`,
`{"documents": [...], "patch": [...], "generated_at": ...}`, каждая
запись — `{"path", "sha256", "size", "updated_at"}` (+`"seq"`/`"version"`/
`"date"` у патчей). Обновляется ИНКРЕМЕНТАЛЬНО при каждом добавлении/
изменении/удалении файла через эту вкладку — не полным пересчётом с
перекачкой всего дерева обратно (hash уже известен из локальных байт
в момент загрузки, скачивать файл заново, чтобы его перехешировать, не
нужно). Никакой отдельной версии у самих папок нет — по прямому
решению пользователя ("я не знаю" на вопрос про версионирование папок,
решение — не городить третью независимую систему версий поверх (а)
версии самой сборки и (б) версии, зашитой в имя каждого патч-файла):
"актуальность" `documents/` определяется содержимым файлов + их sha256
в манифесте, у патчей — обычным порядком применения по имени файла.

## Нэйминг патчей

Прямой ответ на вопрос: порядковый номер (4 цифры, `0001`..`9999` —
этого достаточно с большим запасом для количества патчей, которое
реально когда-либо потребуется одной сборке) + версия + дата,
например `0001_v1.0.3_2026-09-24.bat`. Номер выдаётся автоматически при
добавлении (максимум уже существующих + 1, никогда не переиспользуется
после удаления) — оператору нужно ввести только версию, дата
подставляется текущая. Это по-прежнему только МЕТКА создания (имя файла
должно быть уникальным, и по нему видно, в каком порядке патчи
появились) — реальный порядок ПРИМЕНЕНИЯ читайте ниже, "Очередь
применения патчей и включение/выключение", он с этим номером больше не
обязан совпадать.

## Очередь применения патчей и включение/выключение (2026-09-28)

Прямой запрос: чекбоксы у патчей И у документов (шаблонов .ini) — чтобы
явно выбирать, что лаунчер реально применяет, а что просто лежит на
сервере про запас/на будущее; плюс возможность настроить порядок
применения патчей руками, а не мириться с тем, что дала нумерация имени
файла при создании.

**Это меняет ранее описанный здесь контракт** ("лаунчер применяет патчи
строго в порядке лексикографической сортировки имени файла") — тот
вариант был рассчитан на состояние ДО этой правки, когда порядка,
отдельного от имени файла, не существовало. Теперь у каждой записи
`extras_manifest.json` (и `documents`, и `patch`) есть булево поле
`"enabled"` (по умолчанию `true` — лаунчер должен молча применять
запись без этого поля вообще, если когда-нибудь получит манифест от
версии менеджера старше этой правки), и у `patch`-записей — ЯВНОЕ целое
поле `"order"` (плотное, от 0, без дырок в норме) — это и есть реальный
порядок применения, задаётся кнопками "🔼 Выше"/"🔽 Ниже" в
`_move_patch()`, НЕ парсингом имени файла. Выбор в пользу отдельного
поля манифеста, а не переименования файла при реордере: реордер тогда
— это правка одного маленького JSON (уже и так перезаписывается на
каждое действие в этой вкладке), без повторной закачки/удаления самих
`.bat`-файлов ради одной лишь смены места в очереди.

**Лаунчер, когда будет читать `extras_manifest.json`, должен**: (1)
пропускать любую запись (`documents`/`patch`) с `"enabled": false`; (2)
для `patch` — сортировать оставшиеся (`enabled` истинно или поле
отсутствует) записи по `"order"` по возрастанию; запись БЕЗ поля
`"order"` вообще (легаси, до этой правки, или файл на сервере есть, а
в манифесте почему-то нет) — в конец очереди, отсортированными между
собой по имени файла (тот же принцип, что и `_load_patches()` здесь
использует для отображения, см. её собственный комментарий) — никогда
не встраивать такую запись между двумя, у которых `"order"` задан явно.
Для `documents` порядок применения не имеет значения (каждый файл
кладётся по своему собственному пути, между ними нет последовательной
зависимости, как у `.bat`-патчей) — важен только `"enabled"`.

## `patchs/` — вспомогательные файлы для патчей (2026-09-28)

Прямой запрос: отдельная папка, куда класть файлы, которые сам патч
использует (например .dll для DLSS5 — упомянуто в том же запросе как
предстоящая задача: "позже подготовлю 2 python/bat скрипта патча для
включения DLSS5, 1 на AMD и 2 на NVIDIA" — эти скрипты, когда появятся,
пойдут через обычный `patch/` (см. выше), а сюда — их DLL/конфиг-
зависимости, на которые скрипт будет ссылаться относительным путём
внутри архива сборки, например `patchs\nvngx_dlss.dll`). **Именование
"patchs" (не "patches")** — так было явно написано в запросе; сохранён
буквально, а не "исправлен" в сторону обычного английского множественного
числа, раз это имя каталога — часть публичного контракта, который
лаунчер (см. `scp-oss/TESL`) будет читать по фиксированному пути.

В отличие от `documents/`/`patch/` — БЕЗ отдельных записей в
`extras_manifest.json`: у этих файлов нет своего "enabled"/"order",
они не применяются сами по себе, только упоминаются ИЗ `.bat`/`.py`
патча (тот факт, использует ли конкретный патч конкретный файл отсюда —
целиком внутри самого содержимого патч-скрипта, не факт, который эта
вкладка обязана знать или отслеживать). Простое хранилище — показать/
добавить/удалить, тот же generic PUT/GET/DELETE, что и везде в этом
файле.
"""
import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QMessageBox, QFileDialog,
    QInputDialog, QScrollArea, QFrame,
)
from PyQt6.QtCore import Qt, pyqtSignal

from depot_files_tab import FileEditDialog, _fmt_size
from ini_editor import IniEditorDialog

MANIFEST_PATH = "extras_manifest.json"
DOCS_PREFIX   = "documents/"
PATCH_PREFIX  = "patch/"
PATCHFILES_PREFIX = "patchs/"

_PATCH_SEQ_RE = re.compile(r"^(\d+)_")
_SANITIZE_RE  = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize(s: str) -> str:
    return _SANITIZE_RE.sub("-", s.strip()) or "x"


# ── extras_manifest.json — read/mutate/write helpers ────────────────────────

def _load_manifest(client) -> dict:
    data = client.get_bytes(MANIFEST_PATH)
    if data is None:
        return {"documents": [], "patch": []}
    try:
        m = json.loads(data.decode("utf-8"))
    except Exception:
        m = {}
    m.setdefault("documents", [])
    m.setdefault("patch", [])
    return m


def _save_manifest(client, manifest: dict) -> bool:
    manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    return client.put(MANIFEST_PATH, payload, content_type="application/json")


def _upsert_entry(manifest: dict, category: str, path: str, data: bytes, extra: dict = None):
    entries = manifest[category]
    # Живая запись может уже существовать под этим путём (правка файла,
    # не создание) — "enabled"/"order" относятся к оператору, не к
    # содержимому файла, их нельзя молча сбрасывать в дефолт на каждое
    # сохранение (иначе правка .ini выключенного шаблона незаметно
    # включила бы его обратно). Переносим то, что уже было, ДО удаления
    # старой записи — `extra` (если передан) всё равно может явно
    # переопределить любое из них (см. _add_patch()'s "order").
    old = next((e for e in entries if e.get("path") == path), None)
    entries[:] = [e for e in entries if e.get("path") != path]
    entry = {
        "path":       path,
        "sha256":     hashlib.sha256(data).hexdigest(),
        "size":       len(data),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "enabled":    old.get("enabled", True) if old else True,
    }
    if old and "order" in old:
        entry["order"] = old["order"]
    if extra:
        entry.update(extra)
    entries.append(entry)


def _remove_entry(manifest: dict, category: str, path: str):
    manifest[category] = [e for e in manifest[category] if e.get("path") != path]


def _next_order(manifest: dict, category: str) -> int:
    """Следующее свободное место в очереди применения — максимум уже
    занятых order (легаси-записи без order считаются по своему индексу
    в списке, чтобы не столкнуться с реальным номером) + 1, 0 если
    список пуст."""
    orders = [e.get("order", i) for i, e in enumerate(manifest.get(category, []))]
    return (max(orders) + 1) if orders else 0


# ── DocumentsTab ──────────────────────────────────────────────────────────────

class DocumentsTab(QWidget):
    log_message = pyqtSignal(str)

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._client = None
        # Гасят itemChanged() во время программного заполнения таблицы
        # (setItem()/setCheckState() на каждой строке при _load_documents()/
        # _load_patches() иначе выглядели бы как N кликов пользователя по
        # чекбоксу и сделали бы N лишних PUT extras_manifest.json подряд).
        self._loading_docs   = False
        self._loading_patches = False
        self._init_ui()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _init_ui(self):
        # Три блока (Документы/Патчи/Файлы патчей) на минимальном размере
        # окна (900×620) уже не помещаются без прокрутки — тот же класс
        # layout-бага, что этот проект чинил раньше (см. main_window.py's
        # _build_settings_tab(), CLAUDE.md "Скукуренные строки" — там та же
        # QScrollArea-обёртка). Сам DocumentsTab остаётся тем же QWidget,
        # что main_window.py передаёт в _add_page() — внутри просто прокладка
        # через QScrollArea до реального контента, никаких изменений снаружи.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setSpacing(8)

        conn_box = QGroupBox("Сборка")
        conn_row = QHBoxLayout(conn_box)
        self.build_hint_label = QLabel("Сборка не выбрана")
        conn_row.addWidget(self.build_hint_label)
        conn_row.addStretch()
        hint = QLabel(
            "Сборка выбирается вверху окна. Общие на всю сборку — не "
            "привязаны к конкретному компоненту"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; font-size: 9pt;")
        conn_row.addWidget(hint, stretch=1)
        root.addWidget(conn_box)

        # ── Документы ────────────────────────────────────────────────────────
        doc_box = QGroupBox("📄 Шаблоны настроек (documents/)")
        doc_layout = QVBoxLayout(doc_box)
        doc_toolbar = QHBoxLayout()
        self.btn_doc_load = QPushButton("📥 Показать")
        self.btn_doc_load.clicked.connect(self._load_documents)
        doc_toolbar.addWidget(self.btn_doc_load)
        self.btn_doc_add = QPushButton("➕ Добавить файл…")
        self.btn_doc_add.clicked.connect(self._add_document)
        doc_toolbar.addWidget(self.btn_doc_add)
        self.btn_doc_edit = QPushButton("✏️ Редактировать")
        self.btn_doc_edit.setEnabled(False)
        self.btn_doc_edit.clicked.connect(self._edit_document)
        doc_toolbar.addWidget(self.btn_doc_edit)
        self.btn_doc_delete = QPushButton("🗑 Удалить")
        self.btn_doc_delete.setEnabled(False)
        self.btn_doc_delete.clicked.connect(self._delete_document)
        doc_toolbar.addWidget(self.btn_doc_delete)
        doc_toolbar.addStretch()
        doc_layout.addLayout(doc_toolbar)

        self.doc_table = QTableWidget(0, 3)
        self.doc_table.setHorizontalHeaderLabels(["✓", "Файл", "Размер"])
        self.doc_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.doc_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.doc_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.doc_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # NoEditTriggers всё ещё запрещает редактирование ТЕКСТА ячеек —
        # чекбокс переключается кликом по самому чекбоксу (ItemIsUserCheckable),
        # это не "редактирование" в смысле EditTrigger, оно продолжает работать.
        self.doc_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.doc_table.verticalHeader().setVisible(False)
        self.doc_table.itemSelectionChanged.connect(self._on_doc_selection)
        self.doc_table.itemDoubleClicked.connect(lambda _: self._edit_document())
        self.doc_table.itemChanged.connect(self._on_doc_item_changed)
        doc_layout.addWidget(self.doc_table)
        doc_hint = QLabel("✓ — применять ли шаблон в лаунчере (снятая галка не удаляет файл с сервера)")
        doc_hint.setWordWrap(True)
        doc_hint.setStyleSheet("color: #888; font-size: 9pt;")
        doc_layout.addWidget(doc_hint)
        root.addWidget(doc_box)

        # ── Патчи ────────────────────────────────────────────────────────────
        patch_box = QGroupBox("🩹 Патчи (patch/)")
        patch_layout = QVBoxLayout(patch_box)
        patch_toolbar = QHBoxLayout()
        self.btn_patch_load = QPushButton("📥 Показать")
        self.btn_patch_load.clicked.connect(self._load_patches)
        patch_toolbar.addWidget(self.btn_patch_load)
        self.btn_patch_add = QPushButton("➕ Добавить патч…")
        self.btn_patch_add.clicked.connect(self._add_patch)
        patch_toolbar.addWidget(self.btn_patch_add)
        self.btn_patch_up = QPushButton("🔼 Выше")
        self.btn_patch_up.setEnabled(False)
        self.btn_patch_up.setToolTip("Поднять в очереди применения (раньше остальных)")
        self.btn_patch_up.clicked.connect(lambda: self._move_patch(-1))
        patch_toolbar.addWidget(self.btn_patch_up)
        self.btn_patch_down = QPushButton("🔽 Ниже")
        self.btn_patch_down.setEnabled(False)
        self.btn_patch_down.setToolTip("Опустить в очереди применения (позже остальных)")
        self.btn_patch_down.clicked.connect(lambda: self._move_patch(1))
        patch_toolbar.addWidget(self.btn_patch_down)
        self.btn_patch_delete = QPushButton("🗑 Удалить")
        self.btn_patch_delete.setEnabled(False)
        self.btn_patch_delete.clicked.connect(self._delete_patch)
        patch_toolbar.addWidget(self.btn_patch_delete)
        patch_toolbar.addStretch()
        patch_layout.addLayout(patch_toolbar)

        self.patch_table = QTableWidget(0, 5)
        self.patch_table.setHorizontalHeaderLabels(["✓", "#", "Файл", "Размер", "Версия / дата"])
        self.patch_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.patch_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.patch_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.patch_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.patch_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.patch_table.verticalHeader().setVisible(False)
        self.patch_table.itemSelectionChanged.connect(self._on_patch_selection)
        self.patch_table.itemChanged.connect(self._on_patch_item_changed)
        patch_layout.addWidget(self.patch_table)
        patch_hint = QLabel(
            "Применяются лаунчером по очереди. ✓ — применять ли патч. "
            "# — фактический порядок применения (не номер в имени файла) "
            "— «🔼 Выше»/«🔽 Ниже» меняют именно его."
        )
        patch_hint.setWordWrap(True)
        patch_hint.setStyleSheet("color: #888; font-size: 9pt;")
        patch_layout.addWidget(patch_hint)
        root.addWidget(patch_box)

        # ── Файлы патчей ─────────────────────────────────────────────────────
        pf_box = QGroupBox("📦 Файлы патчей (patchs/)")
        pf_layout = QVBoxLayout(pf_box)
        pf_toolbar = QHBoxLayout()
        self.btn_pf_load = QPushButton("📥 Показать")
        self.btn_pf_load.clicked.connect(self._load_patchfiles)
        pf_toolbar.addWidget(self.btn_pf_load)
        self.btn_pf_add = QPushButton("➕ Добавить файл…")
        self.btn_pf_add.clicked.connect(self._add_patchfile)
        pf_toolbar.addWidget(self.btn_pf_add)
        self.btn_pf_delete = QPushButton("🗑 Удалить")
        self.btn_pf_delete.setEnabled(False)
        self.btn_pf_delete.clicked.connect(self._delete_patchfile)
        pf_toolbar.addWidget(self.btn_pf_delete)
        pf_toolbar.addStretch()
        pf_layout.addLayout(pf_toolbar)

        self.pf_table = QTableWidget(0, 2)
        self.pf_table.setHorizontalHeaderLabels(["Файл", "Размер"])
        self.pf_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.pf_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.pf_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.pf_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pf_table.verticalHeader().setVisible(False)
        self.pf_table.itemSelectionChanged.connect(self._on_pf_selection)
        pf_layout.addWidget(self.pf_table)
        pf_hint = QLabel(
            "DLL/конфиги для .bat/.py-патчей. Просто хранилище — сами "
            "файлы никак не применяются, их должен явно упомянуть код "
            "внутри патча (например «patchs\\nvngx_dlss.dll»)."
        )
        pf_hint.setWordWrap(True)
        pf_hint.setStyleSheet("color: #888; font-size: 9pt;")
        pf_layout.addWidget(pf_hint)
        root.addWidget(pf_box)

        self.status_label = QLabel("Выберите сборку вверху окна и нажмите «Показать»")
        self.status_label.setStyleSheet("font-size: 9pt; color: #888;")
        root.addWidget(self.status_label)

    # ── Client / build helpers ──────────────────────────────────────────────

    def _panel_cfg(self) -> dict:
        return self.mw.file_selector.config.get("panel", {})

    def _make_client(self, build_id: str):
        from panel_client import PanelHTTP
        cfg = self._panel_cfg()
        return PanelHTTP(
            base_url   = cfg.get("base_url", "").rstrip("/"),
            build_id   = build_id,
            token      = cfg.get("token", ""),
            verify_ssl = cfg.get("verify_ssl", True),
        )

    def _current_client(self):
        build_id = self.mw.current_build_id()
        if not build_id:
            return None
        return self._make_client(build_id)

    def _update_build_hint(self):
        name = self.mw.current_build_name()
        self.build_hint_label.setText(f"Сборка: {name}" if name else "Сборка не выбрана")

    # ── Включено/выключено (чекбоксы) ───────────────────────────────────────

    def _set_entry_enabled(self, category: str, path: str, enabled: bool):
        client = self._current_client()
        if client is None:
            return
        manifest = _load_manifest(client)
        for e in manifest.get(category, []):
            if e.get("path") == path:
                e["enabled"] = enabled
                break
        else:
            # Файл реально есть на сервере, но в манифесте почему-то нет
            # записи (например когда-то попал туда мимо этой вкладки) —
            # заводим минимальную запись только с enabled; sha256/size
            # подставятся сами при следующем реальном upsert (правка/
            # переупаковка) через эту вкладку.
            manifest.setdefault(category, []).append({"path": path, "enabled": enabled})
        _save_manifest(client, manifest)
        client.close()

    def _on_doc_item_changed(self, item):
        if self._loading_docs or item.column() != 0:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        self._set_entry_enabled("documents", path, item.checkState() == Qt.CheckState.Checked)
        self.log_message.emit(f"{'✅' if item.checkState() == Qt.CheckState.Checked else '⬜'} {path}")

    def _on_patch_item_changed(self, item):
        if self._loading_patches or item.column() != 0:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        if not path:
            return
        self._set_entry_enabled("patch", path, item.checkState() == Qt.CheckState.Checked)
        self.log_message.emit(f"{'✅' if item.checkState() == Qt.CheckState.Checked else '⬜'} {path}")

    def showEvent(self, event):
        super().showEvent(event)
        self._update_build_hint()
        if self.doc_table.rowCount() == 0 and self.patch_table.rowCount() == 0 and self.mw.current_build_id():
            self._load_documents()
            self._load_patches()
            self._load_patchfiles()

    def on_build_changed(self):
        """MainWindow._notify_build_changed() — см. main_window.py. Тот же
        принцип, что и в depot_files_tab.py::on_build_changed() — сбрасываем
        содержимое (относилось к предыдущей сборке) и перезагружаем сразу,
        только если страница реально видна."""
        self._update_build_hint()
        self.doc_table.setRowCount(0)
        self.patch_table.setRowCount(0)
        self.pf_table.setRowCount(0)
        if not self.mw.current_build_id():
            self.status_label.setText("Сборка не выбрана — выберите её вверху окна")
            return
        if self.isVisible():
            self._load_documents()
            self._load_patches()
            self._load_patchfiles()
        else:
            self.status_label.setText("Выберите сборку вверху окна и нажмите «Показать»")

    # ── Документы ────────────────────────────────────────────────────────────

    def _load_documents(self):
        client = self._current_client()
        if client is None:
            QMessageBox.warning(self, "Ошибка", "Выберите сборку")
            return
        files = client.list_files()
        manifest = _load_manifest(client)
        client.close()
        if files is None:
            self.status_label.setText("❌ Не удалось получить список файлов")
            return
        manifest_by_path = {e.get("path"): e for e in manifest.get("documents", [])}
        docs = sorted(
            (f for f in files if f["path"].startswith(DOCS_PREFIX) and f["path"] != DOCS_PREFIX),
            key=lambda f: f["path"],
        )
        self._loading_docs = True
        self.doc_table.setRowCount(0)
        for f in docs:
            entry = manifest_by_path.get(f["path"], {})
            row = self.doc_table.rowCount()
            self.doc_table.insertRow(row)

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(Qt.CheckState.Checked if entry.get("enabled", True) else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, f["path"])
            self.doc_table.setItem(row, 0, chk)

            self.doc_table.setItem(row, 1, QTableWidgetItem(f["path"][len(DOCS_PREFIX):]))
            self.doc_table.setItem(row, 2, QTableWidgetItem(_fmt_size(f["size"])))
        self._loading_docs = False
        self.status_label.setText(f"Шаблонов: {len(docs)}   Патчей: {self.patch_table.rowCount()}")
        self.log_message.emit(f"📄 Шаблонов настроек: {len(docs)}")

    def _on_doc_selection(self):
        has_sel = bool(self.doc_table.selectedItems())
        self.btn_doc_edit.setEnabled(has_sel)
        self.btn_doc_delete.setEnabled(has_sel)

    def _selected_doc_path(self):
        row = self.doc_table.currentRow()
        if row < 0:
            return None
        item = self.doc_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _add_document(self):
        client = self._current_client()
        if client is None:
            QMessageBox.warning(self, "Ошибка", "Выберите сборку")
            return
        local_path, _ = QFileDialog.getOpenFileName(self, "Выберите файл шаблона настроек")
        if not local_path:
            client.close()
            return
        rel_path = DOCS_PREFIX + Path(local_path).name
        data = Path(local_path).read_bytes()
        if not client.put(rel_path, data):
            QMessageBox.warning(self, "Ошибка", f"Не удалось загрузить {rel_path}")
            client.close()
            return
        manifest = _load_manifest(client)
        _upsert_entry(manifest, "documents", rel_path, data)
        _save_manifest(client, manifest)
        client.close()
        self.log_message.emit(f"✅ Загружен шаблон: {rel_path}")
        self._load_documents()

    def _edit_document(self):
        rel_path = self._selected_doc_path()
        if not rel_path:
            return
        client = self._current_client()
        if client is None:
            return
        data = client.get_bytes(rel_path)
        if data is None:
            QMessageBox.warning(self, "Ошибка", "Файл не найден на сервере")
            client.close()
            return

        if rel_path.lower().endswith(".ini"):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("cp1251", errors="replace")
            dlg = IniEditorDialog(self, rel_path, text)
            if dlg.exec() != dlg.DialogCode.Accepted or dlg._entries is None:
                client.close()
                return
            new_data = dlg.get_text().encode("utf-8")
        else:
            dlg = FileEditDialog(self, client, rel_path)
            if dlg.exec() != dlg.DialogCode.Accepted:
                client.close()
                return
            # FileEditDialog уже сохранило через client.put() само —
            # перечитываем итоговые байты, чтобы обновить манифест тем же
            # содержимым, что реально легло на сервер.
            new_data = client.get_bytes(rel_path)
            if new_data is None:
                client.close()
                self._load_documents()
                return
            manifest = _load_manifest(client)
            _upsert_entry(manifest, "documents", rel_path, new_data)
            _save_manifest(client, manifest)
            client.close()
            self.log_message.emit(f"✅ Сохранено: {rel_path}")
            self._load_documents()
            return

        if not client.put(rel_path, new_data):
            QMessageBox.warning(self, "Ошибка", f"Не удалось сохранить {rel_path}")
            client.close()
            return
        manifest = _load_manifest(client)
        _upsert_entry(manifest, "documents", rel_path, new_data)
        _save_manifest(client, manifest)
        client.close()
        self.log_message.emit(f"✅ Сохранено: {rel_path}")
        self._load_documents()

    def _delete_document(self):
        rel_path = self._selected_doc_path()
        if not rel_path:
            return
        ans = QMessageBox.question(
            self, "Удаление", f"Удалить шаблон <b>{rel_path}</b>?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        client = self._current_client()
        if client is None:
            return
        if not client.delete_object(rel_path):
            QMessageBox.warning(self, "Ошибка", f"Не удалось удалить {rel_path}")
            client.close()
            return
        manifest = _load_manifest(client)
        _remove_entry(manifest, "documents", rel_path)
        _save_manifest(client, manifest)
        client.close()
        self.log_message.emit(f"🗑 Удалён шаблон: {rel_path}")
        self._load_documents()

    # ── Патчи ────────────────────────────────────────────────────────────────

    def _load_patches(self):
        client = self._current_client()
        if client is None:
            QMessageBox.warning(self, "Ошибка", "Выберите сборку")
            return
        files = client.list_files()
        manifest = _load_manifest(client)
        client.close()
        if files is None:
            self.status_label.setText("❌ Не удалось получить список файлов")
            return
        manifest_by_path = {e.get("path"): e for e in manifest.get("patch", [])}
        raw = [f for f in files if f["path"].startswith(PATCH_PREFIX) and f["path"] != PATCH_PREFIX]
        patches = []
        for f in raw:
            entry = manifest_by_path.get(f["path"], {})
            patches.append({
                "path":    f["path"],
                "size":    f["size"],
                "enabled": entry.get("enabled", True),
                "order":   entry.get("order"),
                "version": entry.get("version", ""),
                "date":    entry.get("date", ""),
            })
        # Реальная очередь применения — по "order" из манифеста, см. "Очередь
        # применения патчей" в докстринге модуля. Легаси-записи без него
        # (файл на сервере есть, а в манифесте либо вообще нет записи, либо
        # она осталась от версии менеджера до этой правки) — сортируем между
        # собой по имени файла (старое поведение) и ставим ПОСЛЕ всех, у кого
        # order задан явно, а не как попало между ними.
        with_order    = sorted((p for p in patches if p["order"] is not None), key=lambda p: p["order"])
        without_order = sorted((p for p in patches if p["order"] is None), key=lambda p: p["path"])
        ordered = with_order + without_order

        self._loading_patches = True
        self.patch_table.setRowCount(0)
        for pos, p in enumerate(ordered, start=1):
            name = p["path"][len(PATCH_PREFIX):]
            row = self.patch_table.rowCount()
            self.patch_table.insertRow(row)

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(Qt.CheckState.Checked if p["enabled"] else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, p["path"])
            self.patch_table.setItem(row, 0, chk)

            self.patch_table.setItem(row, 1, QTableWidgetItem(str(pos)))
            name_item = QTableWidgetItem(name)
            self.patch_table.setItem(row, 2, name_item)
            self.patch_table.setItem(row, 3, QTableWidgetItem(_fmt_size(p["size"])))
            # версия/дата — из манифеста, если есть (проставляется при
            # добавлении), иначе разбираем из имени файла как раньше
            # (0001_v1.0.3_2026-09-24.bat) — только для отображения человеку.
            if p["version"] or p["date"]:
                info = " / ".join(x for x in (p["version"], p["date"]) if x)
            else:
                parts = name.rsplit(".", 1)[0].split("_", 2)
                info = " / ".join(parts[1:]) if len(parts) > 1 else ""
            self.patch_table.setItem(row, 4, QTableWidgetItem(info))
        self._loading_patches = False
        self.status_label.setText(f"Шаблонов: {self.doc_table.rowCount()}   Патчей: {len(ordered)}")
        self.log_message.emit(f"🩹 Патчей: {len(ordered)}")

    def _on_patch_selection(self):
        row = self.patch_table.currentRow()
        has_sel = row >= 0
        self.btn_patch_delete.setEnabled(has_sel)
        self.btn_patch_up.setEnabled(has_sel and row > 0)
        self.btn_patch_down.setEnabled(has_sel and row < self.patch_table.rowCount() - 1)

    def _selected_patch_path(self):
        row = self.patch_table.currentRow()
        if row < 0:
            return None
        item = self.patch_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _move_patch(self, delta: int):
        """delta=-1 — поднять (раньше остальных), +1 — опустить. Переставляет
        "order" в манифесте, файлы на сервере не переименовываются (см.
        "Очередь применения патчей" в докстринге модуля за обоснование)."""
        rel_path = self._selected_patch_path()
        if not rel_path:
            return
        client = self._current_client()
        if client is None:
            return
        manifest = _load_manifest(client)
        entries = manifest.get("patch", [])
        # Нормализуем order у ВСЕХ записей в плотную последовательность по
        # их текущему фактическому порядку отображения (включая легаси-
        # записи без order вообще) — иначе своп ниже мог бы дать
        # неоднозначный результат при дырках/дублях в старых номерах.
        entries.sort(key=lambda e: (e.get("order") is None, e.get("order", 0), e.get("path", "")))
        for i, e in enumerate(entries):
            e["order"] = i
        idx = next((i for i, e in enumerate(entries) if e.get("path") == rel_path), None)
        if idx is None:
            client.close()
            return
        new_idx = idx + delta
        if not (0 <= new_idx < len(entries)):
            client.close()
            return
        entries[idx]["order"], entries[new_idx]["order"] = entries[new_idx]["order"], entries[idx]["order"]
        manifest["patch"] = entries
        _save_manifest(client, manifest)
        client.close()
        self.log_message.emit(f"↕️ Порядок патчей изменён: {rel_path}")
        # _load_patches() полностью пересобирает таблицу — без явного
        # восстановления выбора после этого повторный клик "Выше"/"Ниже"
        # (естественный способ подвинуть один и тот же патч на несколько
        # позиций подряд) требовал бы заново кликать по строке между
        # каждым нажатием. Ищем ту же запись по пути в уже пересобранной
        # таблице и восстанавливаем на ней курсор/выделение.
        self._load_patches()
        for row in range(self.patch_table.rowCount()):
            item = self.patch_table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == rel_path:
                self.patch_table.setCurrentCell(row, 0)
                break

    def _next_patch_seq(self, client) -> int:
        files = client.list_files() or []
        max_seq = 0
        for f in files:
            if not f["path"].startswith(PATCH_PREFIX):
                continue
            m = _PATCH_SEQ_RE.match(f["path"][len(PATCH_PREFIX):])
            if m:
                max_seq = max(max_seq, int(m.group(1)))
        return max_seq + 1

    def _add_patch(self):
        client = self._current_client()
        if client is None:
            QMessageBox.warning(self, "Ошибка", "Выберите сборку")
            return
        local_path, _ = QFileDialog.getOpenFileName(
            self, "Выберите .bat файл патча", "", "Batch files (*.bat);;Все файлы (*)"
        )
        if not local_path:
            client.close()
            return
        version, ok = QInputDialog.getText(self, "Версия патча", "Версия (например 1.0.3):")
        if not ok:
            client.close()
            return
        seq = self._next_patch_seq(client)
        version_s = _sanitize(version) if version.strip() else "x"
        date_s = date.today().isoformat()
        filename = f"{seq:04d}_v{version_s}_{date_s}.bat"
        rel_path = PATCH_PREFIX + filename

        ans = QMessageBox.question(
            self, "Добавить патч",
            f"Будет загружен как:\n<b>{rel_path}</b>\n\n"
            f"Добавится последним в очередь применения — переставить его "
            f"можно потом кнопками «🔼 Выше»/«🔽 Ниже». Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            client.close()
            return

        data = Path(local_path).read_bytes()
        if not client.put(rel_path, data):
            QMessageBox.warning(self, "Ошибка", f"Не удалось загрузить {rel_path}")
            client.close()
            return
        manifest = _load_manifest(client)
        order = _next_order(manifest, "patch")
        _upsert_entry(manifest, "patch", rel_path, data, extra={
            "seq": seq, "version": version.strip(), "date": date_s,
            "enabled": True, "order": order,
        })
        _save_manifest(client, manifest)
        client.close()
        self.log_message.emit(f"✅ Загружен патч: {rel_path}")
        self._load_patches()

    def _delete_patch(self):
        rel_path = self._selected_patch_path()
        if not rel_path:
            return
        ans = QMessageBox.question(
            self, "Удаление", f"Удалить патч <b>{rel_path}</b>? Действие необратимо.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        client = self._current_client()
        if client is None:
            return
        if not client.delete_object(rel_path):
            QMessageBox.warning(self, "Ошибка", f"Не удалось удалить {rel_path}")
            client.close()
            return
        manifest = _load_manifest(client)
        _remove_entry(manifest, "patch", rel_path)
        _save_manifest(client, manifest)
        client.close()
        self.log_message.emit(f"🗑 Удалён патч: {rel_path}")
        self._load_patches()

    # ── Файлы патчей (patchs/) ──────────────────────────────────────────────
    # Простое хранилище — без записей в extras_manifest.json, см. докстринг
    # модуля "patchs/ — вспомогательные файлы для патчей" за обоснование.

    def _load_patchfiles(self):
        client = self._current_client()
        if client is None:
            QMessageBox.warning(self, "Ошибка", "Выберите сборку")
            return
        files = client.list_files()
        client.close()
        if files is None:
            self.status_label.setText("❌ Не удалось получить список файлов")
            return
        entries = sorted(
            (f for f in files if f["path"].startswith(PATCHFILES_PREFIX) and f["path"] != PATCHFILES_PREFIX),
            key=lambda f: f["path"],
        )
        self.pf_table.setRowCount(0)
        for f in entries:
            row = self.pf_table.rowCount()
            self.pf_table.insertRow(row)
            name_item = QTableWidgetItem(f["path"][len(PATCHFILES_PREFIX):])
            name_item.setData(Qt.ItemDataRole.UserRole, f["path"])
            self.pf_table.setItem(row, 0, name_item)
            self.pf_table.setItem(row, 1, QTableWidgetItem(_fmt_size(f["size"])))
        self.log_message.emit(f"📦 Файлов патчей: {len(entries)}")

    def _on_pf_selection(self):
        self.btn_pf_delete.setEnabled(bool(self.pf_table.selectedItems()))

    def _selected_pf_path(self):
        row = self.pf_table.currentRow()
        if row < 0:
            return None
        item = self.pf_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _add_patchfile(self):
        client = self._current_client()
        if client is None:
            QMessageBox.warning(self, "Ошибка", "Выберите сборку")
            return
        local_path, _ = QFileDialog.getOpenFileName(self, "Выберите файл для патчей (например .dll)")
        if not local_path:
            client.close()
            return
        rel_path = PATCHFILES_PREFIX + Path(local_path).name
        data = Path(local_path).read_bytes()
        ok = client.put(rel_path, data)
        client.close()
        if not ok:
            QMessageBox.warning(self, "Ошибка", f"Не удалось загрузить {rel_path}")
            return
        self.log_message.emit(f"✅ Загружен файл патча: {rel_path}")
        self._load_patchfiles()

    def _delete_patchfile(self):
        rel_path = self._selected_pf_path()
        if not rel_path:
            return
        ans = QMessageBox.question(
            self, "Удаление", f"Удалить файл <b>{rel_path}</b>? Действие необратимо.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        client = self._current_client()
        if client is None:
            return
        ok = client.delete_object(rel_path)
        client.close()
        if not ok:
            QMessageBox.warning(self, "Ошибка", f"Не удалось удалить {rel_path}")
            return
        self.log_message.emit(f"🗑 Удалён файл патча: {rel_path}")
        self._load_patchfiles()
