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
например `0001_v1.0.3_2026-09-24.bat`. Лаунчер (когда до него дойдёт
очередь) применяет патчи строго в порядке ЛЕКСИКОГРАФИЧЕСКОЙ сортировки
имени файла — не парсит дату/версию из имени для определения порядка,
они там только для человека. Номер выдаётся автоматически при
добавлении (максимум уже существующих + 1) — оператору нужно ввести
только версию, дата подставляется текущая.
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
    QInputDialog,
)
from PyQt6.QtCore import Qt, pyqtSignal

from depot_files_tab import FileEditDialog, _fmt_size
from ini_editor import IniEditorDialog

MANIFEST_PATH = "extras_manifest.json"
DOCS_PREFIX   = "documents/"
PATCH_PREFIX  = "patch/"

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
    entries[:] = [e for e in entries if e.get("path") != path]
    entry = {
        "path":       path,
        "sha256":     hashlib.sha256(data).hexdigest(),
        "size":       len(data),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        entry.update(extra)
    entries.append(entry)


def _remove_entry(manifest: dict, category: str, path: str):
    manifest[category] = [e for e in manifest[category] if e.get("path") != path]


# ── DocumentsTab ──────────────────────────────────────────────────────────────

class DocumentsTab(QWidget):
    log_message = pyqtSignal(str)

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._client = None
        self._init_ui()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _init_ui(self):
        root = QVBoxLayout(self)
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
        hint.setStyleSheet("color: #888; font-size: 9pt;")
        conn_row.addWidget(hint)
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

        self.doc_table = QTableWidget(0, 2)
        self.doc_table.setHorizontalHeaderLabels(["Файл", "Размер"])
        self.doc_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.doc_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.doc_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.doc_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.doc_table.verticalHeader().setVisible(False)
        self.doc_table.itemSelectionChanged.connect(self._on_doc_selection)
        self.doc_table.itemDoubleClicked.connect(lambda _: self._edit_document())
        doc_layout.addWidget(self.doc_table)
        root.addWidget(doc_box)

        # ── Патчи ────────────────────────────────────────────────────────────
        patch_box = QGroupBox("🩹 Патчи (patch/) — применяются лаунчером по порядку имени файла")
        patch_layout = QVBoxLayout(patch_box)
        patch_toolbar = QHBoxLayout()
        self.btn_patch_load = QPushButton("📥 Показать")
        self.btn_patch_load.clicked.connect(self._load_patches)
        patch_toolbar.addWidget(self.btn_patch_load)
        self.btn_patch_add = QPushButton("➕ Добавить патч…")
        self.btn_patch_add.clicked.connect(self._add_patch)
        patch_toolbar.addWidget(self.btn_patch_add)
        self.btn_patch_delete = QPushButton("🗑 Удалить")
        self.btn_patch_delete.setEnabled(False)
        self.btn_patch_delete.clicked.connect(self._delete_patch)
        patch_toolbar.addWidget(self.btn_patch_delete)
        patch_toolbar.addStretch()
        patch_layout.addLayout(patch_toolbar)

        self.patch_table = QTableWidget(0, 4)
        self.patch_table.setHorizontalHeaderLabels(["#", "Файл", "Размер", "Версия / дата"])
        self.patch_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.patch_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.patch_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.patch_table.verticalHeader().setVisible(False)
        self.patch_table.itemSelectionChanged.connect(self._on_patch_selection)
        patch_layout.addWidget(self.patch_table)
        root.addWidget(patch_box)

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

    def showEvent(self, event):
        super().showEvent(event)
        self._update_build_hint()
        if self.doc_table.rowCount() == 0 and self.patch_table.rowCount() == 0 and self.mw.current_build_id():
            self._load_documents()
            self._load_patches()

    def on_build_changed(self):
        """MainWindow._notify_build_changed() — см. main_window.py. Тот же
        принцип, что и в depot_files_tab.py::on_build_changed() — сбрасываем
        содержимое (относилось к предыдущей сборке) и перезагружаем сразу,
        только если страница реально видна."""
        self._update_build_hint()
        self.doc_table.setRowCount(0)
        self.patch_table.setRowCount(0)
        if not self.mw.current_build_id():
            self.status_label.setText("Сборка не выбрана — выберите её вверху окна")
            return
        if self.isVisible():
            self._load_documents()
            self._load_patches()
        else:
            self.status_label.setText("Выберите сборку вверху окна и нажмите «Показать»")

    # ── Документы ────────────────────────────────────────────────────────────

    def _load_documents(self):
        client = self._current_client()
        if client is None:
            QMessageBox.warning(self, "Ошибка", "Выберите сборку")
            return
        files = client.list_files()
        client.close()
        if files is None:
            self.status_label.setText("❌ Не удалось получить список файлов")
            return
        self.doc_table.setRowCount(0)
        docs = sorted(
            (f for f in files if f["path"].startswith(DOCS_PREFIX) and f["path"] != DOCS_PREFIX),
            key=lambda f: f["path"],
        )
        for f in docs:
            row = self.doc_table.rowCount()
            self.doc_table.insertRow(row)
            name_item = QTableWidgetItem(f["path"][len(DOCS_PREFIX):])
            name_item.setData(Qt.ItemDataRole.UserRole, f["path"])
            self.doc_table.setItem(row, 0, name_item)
            self.doc_table.setItem(row, 1, QTableWidgetItem(_fmt_size(f["size"])))
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
        return self.doc_table.item(row, 0).data(Qt.ItemDataRole.UserRole)

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
        client.close()
        if files is None:
            self.status_label.setText("❌ Не удалось получить список файлов")
            return
        self.patch_table.setRowCount(0)
        patches = sorted(
            (f for f in files if f["path"].startswith(PATCH_PREFIX) and f["path"] != PATCH_PREFIX),
            key=lambda f: f["path"],
        )
        for f in patches:
            name = f["path"][len(PATCH_PREFIX):]
            m = _PATCH_SEQ_RE.match(name)
            seq = m.group(1) if m else "?"
            row = self.patch_table.rowCount()
            self.patch_table.insertRow(row)
            self.patch_table.setItem(row, 0, QTableWidgetItem(seq))
            name_item = QTableWidgetItem(name)
            name_item.setData(Qt.ItemDataRole.UserRole, f["path"])
            self.patch_table.setItem(row, 1, name_item)
            self.patch_table.setItem(row, 2, QTableWidgetItem(_fmt_size(f["size"])))
            # версия/дата — из имени файла (0001_v1.0.3_2026-09-24.bat),
            # только для отображения человеку, порядок применения по-прежнему
            # определяется лексикографической сортировкой самого имени файла.
            parts = name.rsplit(".", 1)[0].split("_", 2)
            info = " / ".join(parts[1:]) if len(parts) > 1 else ""
            self.patch_table.setItem(row, 3, QTableWidgetItem(info))
        self.status_label.setText(f"Шаблонов: {self.doc_table.rowCount()}   Патчей: {len(patches)}")
        self.log_message.emit(f"🩹 Патчей: {len(patches)}")

    def _on_patch_selection(self):
        self.btn_patch_delete.setEnabled(bool(self.patch_table.selectedItems()))

    def _selected_patch_path(self):
        row = self.patch_table.currentRow()
        if row < 0:
            return None
        return self.patch_table.item(row, 1).data(Qt.ItemDataRole.UserRole)

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
            f"Порядок применения в лаунчере определяется именно этим "
            f"номером — продолжить?",
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
        _upsert_entry(manifest, "patch", rel_path, data, extra={
            "seq": seq, "version": version.strip(), "date": date_s,
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
