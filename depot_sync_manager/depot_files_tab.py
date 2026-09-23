# ==================== depot_files_tab.py ====================
"""
Часть страницы "🗂️ Файлы на сервере" (см. files_tab.py) — просмотр/
редактирование/добавление уже опубликованных файлов на TESL-Panel (см.
scp-oss/TESL-Panel), десктоп-эквивалент её же
/admin/project/<name>/files (файловый менеджер в браузере) — тот же
Bearer-токен, что и публикация из "🚀 Релизы". Настройки соединения
(URL/токен) читаются из общего cfg["panel"] (настраивается на странице
"⚙️ Настройки", единожды на всё приложение) — здесь настраивается
только КАКОЙ проект просматривать (свой отдельный выбор, не обязательно
совпадающий с текущим проектом публикации — админ может смотреть файлы
любого проекта).

Только для backend="panel" — у WebDAV уже есть своя вкладка "🗂️ Файлы
сервера" (server_files_tab.py, PROPFIND-дерево), эта — параллельный
путь под новый транспорт, не замена.

Редактирование инлайн — только для небольших (<= panel_client'а
MAX_INLINE_EDIT_BYTES) и декодируемых как UTF-8 файлов, тот же принцип,
что и в самой панели (panel/storage.py::MAX_INLINE_EDIT_BYTES) — для
остального доступны скачать/заменить целиком/удалить.
"""
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QComboBox, QLineEdit, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QMessageBox, QFileDialog, QDialog,
    QDialogButtonBox, QTextEdit, QProgressBar,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt6.QtGui import QFont

MAX_INLINE_EDIT_BYTES = 256 * 1024


def _fmt_size(n: int) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


# ── Worker'ы ──────────────────────────────────────────────────────────────────

class _ListWorker(QObject):
    finished = pyqtSignal(list)
    error    = pyqtSignal(str)

    def __init__(self, client):
        super().__init__()
        self.client = client

    def run(self):
        files = self.client.list_files()
        if files is None:
            self.error.emit("Не удалось получить список файлов (проверьте проект/токен)")
        else:
            self.finished.emit(files)


class _UploadWorker(QObject):
    finished = pyqtSignal(bool, str)

    def __init__(self, client, rel_path: str, data: bytes):
        super().__init__()
        self.client   = client
        self.rel_path = rel_path
        self.data     = data

    def run(self):
        ok = self.client.put(self.rel_path, self.data)
        self.finished.emit(ok, self.rel_path)


# ── Диалог просмотра/редактирования одного файла ────────────────────────────

class FileEditDialog(QDialog):
    def __init__(self, parent, client, rel_path: str):
        super().__init__(parent)
        self.client   = client
        self.rel_path = rel_path
        self.setWindowTitle(f"Файл — {rel_path}")
        self.resize(720, 520)
        self._init_ui()
        self._load()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        self.info_label = QLabel("Загрузка…")
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        self.text_edit = QTextEdit()
        self.text_edit.setFont(QFont("Consolas", 10))
        layout.addWidget(self.text_edit)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        self.btn_box = btns
        layout.addWidget(btns)

    def _load(self):
        data = self.client.get_bytes(self.rel_path)
        if data is None:
            self.info_label.setText("⚠️ Файл не найден на сервере")
            self.text_edit.setEnabled(False)
            self.btn_box.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
            return
        if len(data) > MAX_INLINE_EDIT_BYTES:
            self.info_label.setText(
                f"⚠️ Файл слишком большой для правки в редакторе ({_fmt_size(len(data))} "
                f"> {_fmt_size(MAX_INLINE_EDIT_BYTES)}) — только просмотр недоступен, "
                f"замените целиком через «⬆ Загрузить»."
            )
            self.text_edit.setEnabled(False)
            self.btn_box.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self.info_label.setText(
                f"⚠️ Бинарный файл ({_fmt_size(len(data))}) — инлайн-правка не имеет смысла, "
                f"замените целиком через «⬆ Загрузить»."
            )
            self.text_edit.setEnabled(False)
            self.btn_box.button(QDialogButtonBox.StandardButton.Save).setEnabled(False)
            return
        self.info_label.setText(f"{self.rel_path}  ({_fmt_size(len(data))})")
        self.text_edit.setPlainText(text)

    def _save(self):
        content = self.text_edit.toPlainText().encode("utf-8")
        if self.client.put(self.rel_path, content):
            self.accept()
        else:
            QMessageBox.warning(self, "Ошибка", "Не удалось сохранить файл на сервере")


# ── DepotFilesTab ─────────────────────────────────────────────────────────────

class DepotFilesTab(QWidget):
    log_message = pyqtSignal(str)

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window
        self._client = None
        self._thread = None
        self._worker = None
        self._init_ui()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        conn_box = QGroupBox("Проект на панели")
        conn_row = QHBoxLayout(conn_box)
        conn_row.addWidget(QLabel("Проект:"))
        self.project_combo = QComboBox()
        self.project_combo.setEditable(False)
        self.project_combo.setMinimumWidth(200)
        conn_row.addWidget(self.project_combo)

        btn_refresh_projects = QPushButton("🔄")
        btn_refresh_projects.setToolTip("Обновить список проектов с сервера")
        btn_refresh_projects.setFixedWidth(36)
        btn_refresh_projects.clicked.connect(self._refresh_projects)
        conn_row.addWidget(btn_refresh_projects)

        conn_row.addStretch()
        hint = QLabel("Соединение (URL панели / токен) настраивается на странице «⚙️ Настройки»")
        hint.setStyleSheet("color: #888; font-size: 9pt;")
        conn_row.addWidget(hint)
        root.addWidget(conn_box)

        toolbar = QHBoxLayout()
        self.btn_load = QPushButton("📥 Показать файлы")
        self.btn_load.clicked.connect(self._load_files)
        toolbar.addWidget(self.btn_load)

        self.btn_view = QPushButton("👁 Просмотреть / редактировать")
        self.btn_view.setEnabled(False)
        self.btn_view.clicked.connect(self._view_selected)
        toolbar.addWidget(self.btn_view)

        self.btn_upload = QPushButton("⬆ Загрузить файл…")
        self.btn_upload.clicked.connect(self._upload_file)
        toolbar.addWidget(self.btn_upload)

        self.btn_delete = QPushButton("🗑 Удалить")
        self.btn_delete.setEnabled(False)
        self.btn_delete.clicked.connect(self._delete_selected)
        toolbar.addWidget(self.btn_delete)

        toolbar.addStretch()
        root.addLayout(toolbar)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.hide()
        self.progress_bar.setFixedHeight(10)
        root.addWidget(self.progress_bar)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Путь", "Размер"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.itemDoubleClicked.connect(lambda _: self._view_selected())
        root.addWidget(self.table)

        self.status_label = QLabel("Выберите проект и нажмите «Показать файлы»")
        self.status_label.setStyleSheet("font-size: 9pt; color: #888;")
        root.addWidget(self.status_label)

    # ── Client / config helpers ─────────────────────────────────────────────

    def _panel_cfg(self) -> dict:
        return self.mw.file_selector.config.get("panel", {})

    def _make_client(self, project: str):
        from panel_client import PanelHTTP
        cfg = self._panel_cfg()
        return PanelHTTP(
            base_url   = cfg.get("base_url", "").rstrip("/"),
            project    = project,
            token      = cfg.get("token", ""),
            verify_ssl = cfg.get("verify_ssl", True),
        )

    def showEvent(self, event):
        super().showEvent(event)
        if self.project_combo.count() == 0:
            self._refresh_projects()

    # ── Projects ─────────────────────────────────────────────────────────────

    def _refresh_projects(self):
        cfg = self._panel_cfg()
        if not cfg.get("base_url"):
            self.status_label.setText(
                "⚠️ Сначала настройте URL панели/токен на странице «⚙️ Настройки»"
            )
            return
        client = self._make_client("")
        names = client.list_projects()
        client.close()
        current = self.project_combo.currentText()
        self.project_combo.clear()
        self.project_combo.addItems(names)
        if current:
            idx = self.project_combo.findText(current)
            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)
        self.log_message.emit(f"📋 Проектов на панели: {len(names)}")

    # ── Load files ───────────────────────────────────────────────────────────

    def _load_files(self):
        project = self.project_combo.currentText().strip()
        if not project:
            QMessageBox.warning(self, "Ошибка", "Выберите проект")
            return
        cfg = self._panel_cfg()
        if not cfg.get("base_url") or not cfg.get("token"):
            QMessageBox.warning(
                self, "Ошибка",
                "Настройте URL панели и upload-токен на странице «⚙️ Настройки»",
            )
            return

        self._client = self._make_client(project)
        self._set_busy(True)
        self.status_label.setText(f"Загружаем список файлов проекта «{project}»…")

        self._worker = _ListWorker(self._client)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._worker.finished.connect(self._on_loaded)
        self._worker.error.connect(self._on_error)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def _on_loaded(self, files: list):
        self._cleanup_thread()
        self._set_busy(False)
        self.table.setRowCount(0)
        for entry in sorted(files, key=lambda e: e["path"]):
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(entry["path"]))
            size_item = QTableWidgetItem(_fmt_size(entry["size"]))
            size_item.setData(Qt.ItemDataRole.UserRole, entry["size"])
            self.table.setItem(row, 1, size_item)
        total_size = sum(e["size"] for e in files)
        self.status_label.setText(f"Файлов: {len(files)}   Общий размер: {_fmt_size(total_size)}")
        self.log_message.emit(f"✅ Файлы депо: {len(files)} ({_fmt_size(total_size)})")

    def _on_error(self, msg: str):
        self._cleanup_thread()
        self._set_busy(False)
        self.status_label.setText(f"❌ {msg}")
        self.log_message.emit(f"❌ {msg}")

    # ── Selection ────────────────────────────────────────────────────────────

    def _on_selection(self):
        has_sel = bool(self.table.selectedItems())
        self.btn_view.setEnabled(has_sel)
        self.btn_delete.setEnabled(has_sel)

    def _selected_path(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        return self.table.item(row, 0).text()

    # ── View / edit ──────────────────────────────────────────────────────────

    def _view_selected(self):
        rel_path = self._selected_path()
        if not rel_path or self._client is None:
            return
        dlg = FileEditDialog(self, self._client, rel_path)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.log_message.emit(f"✅ Сохранено: {rel_path}")
            self._load_files()

    # ── Upload ───────────────────────────────────────────────────────────────

    def _upload_file(self):
        project = self.project_combo.currentText().strip()
        if not project:
            QMessageBox.warning(self, "Ошибка", "Выберите проект")
            return
        local_path, _ = QFileDialog.getOpenFileName(self, "Выберите файл для загрузки")
        if not local_path:
            return
        default_rel = Path(local_path).name
        rel_path = default_rel  # простая схема — кладём в корень проекта,
        # для произвольного пути внутри дерева переименуйте/переместите
        # локально перед выбором либо используйте «🚀 Релизы» для
        # обычной публикации сборки — эта кнопка для отдельных файлов
        # (readme, poster.png и т.п.), не для чанков.

        client = self._make_client(project)
        data = Path(local_path).read_bytes()
        self._set_busy(True)
        self.status_label.setText(f"Загружаем {rel_path}…")

        self._worker = _UploadWorker(client, rel_path, data)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._worker.finished.connect(lambda ok, p: self._on_uploaded(ok, p, client))
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def _on_uploaded(self, ok: bool, rel_path: str, client):
        self._cleanup_thread()
        self._set_busy(False)
        client.close()
        if ok:
            self.log_message.emit(f"✅ Загружено: {rel_path}")
            self._load_files()
        else:
            QMessageBox.warning(self, "Ошибка", f"Не удалось загрузить {rel_path}")

    # ── Delete ───────────────────────────────────────────────────────────────

    def _delete_selected(self):
        rel_path = self._selected_path()
        if not rel_path or self._client is None:
            return
        ans = QMessageBox.question(
            self, "Удаление",
            f"Удалить <b>{rel_path}</b> с сервера? Действие необратимо.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        if self._client.delete_object(rel_path):
            self.log_message.emit(f"🗑 Удалено: {rel_path}")
            self._load_files()
        else:
            QMessageBox.warning(self, "Ошибка", f"Не удалось удалить {rel_path}")

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool):
        self.btn_load.setEnabled(not busy)
        self.btn_upload.setEnabled(not busy)
        self.progress_bar.setVisible(busy)

    def _cleanup_thread(self):
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(2000)
        self._worker = None
        self._thread = None
