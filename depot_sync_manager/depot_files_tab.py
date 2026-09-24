# ==================== depot_files_tab.py ====================
"""
Часть страницы "🗂️ Файлы на сервере" (см. files_tab.py) — просмотр/
редактирование/добавление уже опубликованных файлов на TESL-Panel (см.
scp-oss/TESL-Panel), десктоп-эквивалент её же
/admin/project/<name>/files (файловый менеджер в браузере) — тот же
Bearer-токен, что и публикация из "🚀 Релизы". Настройки соединения
(URL/токен) читаются из общего cfg["panel"] (настраивается на странице
"⚙️ Настройки", единожды на всё приложение).

Сборка (какой проект просматривать) — прямой запрос пользователя
(2026-09-24, "сделай выбор сборки сверху... файлы на сервере
показываются от неё"): раньше здесь был СВОЙ независимый project_combo
(админ мог смотреть файлы ЛЮБОЙ сборки, не только текущей публикации) —
это оказалось источником живой путаницы (см. CLAUDE.md "Файлы на
сервере пустая после успешной публикации" — комбобокс молча выбирал
алфавитно первую сборку с сервера, не ту, что реально публиковалась).
Теперь сборка ВСЕГДА — глобальный выбор сверху окна
(self.mw.current_build_id(), см. main_window.py) — эта страница только
читает его и обновляется через on_build_changed().

Только для backend="panel" — у WebDAV уже есть своя вкладка "🗂️ Файлы
сервера" (server_files_tab.py, PROPFIND-дерево), эта — параллельный
путь под новый транспорт, не замена.

Редактирование инлайн — только для небольших (<= panel_client'а
MAX_INLINE_EDIT_BYTES) и декодируемых как UTF-8 файлов, тот же принцип,
что и в самой панели (panel/storage.py::MAX_INLINE_EDIT_BYTES) — для
остального доступны скачать/заменить целиком/удалить.

## Навигация по папкам (2026-09-24)

Прямой запрос: "файлы на сервере не папкой показываются содержимым
папки а если их больше чем 1 будет как попать в другую" — список файлов
был плоским (полный относительный путь текстом в каждой строке,
`packs/pack-00001.bin`), неудобно при реалистичном количестве файлов
(`chunks/` в неупакованном режиме — сотни/тысячи записей). Теперь
`_FolderView` строит дерево из плоского списка `[{"path","size"}]",
показывает содержимое ТЕКУЩЕЙ папки (по умолчанию — корень): подпапки
первой (📁, агрегированный размер+число файлов), затем файлы этой же
папки (📄, реальный размер); двойной клик по папке входит в неё, кнопка
"⬆ .." (и двойной клик по ней же) — уровнем выше. Строка пути (хлебная
крошка) над таблицей показывает, где сейчас находимся. Выбор/просмотр/
удаление одного файла (`_selected_path()` и всё, что от него зависит)
работают только на строках-ФАЙЛАХ — на папке эти кнопки недоступны.
"""
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem,
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
        self._files_cache = []   # плоский список [{"path","size"}] с сервера
        self._current_dir  = ""  # "" — корень; иначе путь папки без хвостового "/"
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
            "Сборка выбирается вверху окна. Соединение (URL панели / токен) "
            "настраивается на странице «⚙️ Настройки»"
        )
        hint.setStyleSheet("color: #888; font-size: 9pt;")
        conn_row.addWidget(hint)
        root.addWidget(conn_box)

        toolbar = QHBoxLayout()
        self.btn_load = QPushButton("📥 Показать файлы")
        self.btn_load.clicked.connect(self._load_files)
        toolbar.addWidget(self.btn_load)

        self.btn_up = QPushButton("⬆ Вверх")
        self.btn_up.setEnabled(False)
        self.btn_up.setToolTip("Перейти в родительскую папку")
        self.btn_up.clicked.connect(self._navigate_up)
        toolbar.addWidget(self.btn_up)

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

        self.breadcrumb_label = QLabel("📂 / (корень)")
        self.breadcrumb_label.setStyleSheet("font-size: 9pt; color: #aaa;")
        root.addWidget(self.breadcrumb_label)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Имя", "Размер"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.itemDoubleClicked.connect(lambda _: self._on_row_activated())
        root.addWidget(self.table)

        self.status_label = QLabel("Выберите сборку вверху окна и нажмите «Показать файлы»")
        self.status_label.setStyleSheet("font-size: 9pt; color: #888;")
        root.addWidget(self.status_label)

    def _update_build_hint(self):
        name = self.mw.current_build_name()
        self.build_hint_label.setText(f"Сборка: {name}" if name else "Сборка не выбрана")

    # ── Client / config helpers ─────────────────────────────────────────────

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

    def showEvent(self, event):
        super().showEvent(event)
        self._update_build_hint()
        if not self._files_cache and self.mw.current_build_id():
            self._load_files()

    def on_build_changed(self):
        """MainWindow._notify_build_changed() — глобальный выбор сборки (см.
        main_window.py) сменился. Сбрасываем текущую папку (путь внутри
        старой сборки бессмыслен для новой) и обновляем список, но только
        если страница реально видна — иначе просто чистим кэш до
        следующего showEvent (тот же принцип, что раньше был у
        showEvent-only логики, просто теперь запускается и без переключения
        вкладки, если она уже была активна в момент смены сборки)."""
        self._update_build_hint()
        self._current_dir = ""
        self._files_cache = []
        self.table.setRowCount(0)
        build_id = self.mw.current_build_id()
        if not build_id:
            self.status_label.setText("Сборка не выбрана — выберите её вверху окна")
            self.breadcrumb_label.setText("📂 / (корень)")
            self.btn_up.setEnabled(False)
            return
        if self.isVisible():
            self._load_files()
        else:
            self.status_label.setText("Выберите сборку вверху окна и нажмите «Показать файлы»")

    # ── Load files ───────────────────────────────────────────────────────────

    def _load_files(self):
        build_id = self.mw.current_build_id()
        if not build_id:
            QMessageBox.warning(self, "Ошибка", "Выберите сборку вверху окна")
            return
        cfg = self._panel_cfg()
        if not cfg.get("base_url") or not cfg.get("token"):
            QMessageBox.warning(
                self, "Ошибка",
                "Настройте URL панели и upload-токен на странице «⚙️ Настройки»",
            )
            return

        self._client = self._make_client(build_id)
        self._set_busy(True)
        name = self.mw.current_build_name()
        self.status_label.setText(f"Загружаем список файлов сборки «{name}»…")

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
        self._files_cache = files
        # Папка, в которой мы были (если это не первая загрузка), сохраняется —
        # обновление/удаление одного файла не должно выбрасывать обратно в
        # корень, см. докстринг модуля "Навигация по папкам". Если её больше
        # не существует (например единственный файл в ней удалили) —
        # _render_current_dir() просто покажет пустое содержимое с рабочей
        # кнопкой "⬆ Вверх", а не упадёт.
        self._render_current_dir()
        total_size = sum(e["size"] for e in files)
        self.status_label.setText(f"Файлов: {len(files)}   Общий размер: {_fmt_size(total_size)}")
        self.log_message.emit(f"✅ Файлы депо: {len(files)} ({_fmt_size(total_size)})")

    def _on_error(self, msg: str):
        self._cleanup_thread()
        self._set_busy(False)
        self.status_label.setText(f"❌ {msg}")
        self.log_message.emit(f"❌ {msg}")

    # ── Папки ────────────────────────────────────────────────────────────────
    # Список с сервера плоский ([{"path","size"}]) — здесь он представляется
    # как содержимое ТЕКУЩЕЙ папки (self._current_dir): подпапки (агрегат
    # размера/числа файлов) первыми, затем файлы этой же папки. Тип строки
    # хранится в Qt.ItemDataRole.UserRole первой колонки — {"type": "up"|
    # "dir"|"file", "path": ...}.

    def _render_current_dir(self):
        prefix = f"{self._current_dir}/" if self._current_dir else ""
        dirs: dict = {}
        files_here = []
        for entry in self._files_cache:
            path = entry["path"]
            if prefix and not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            if not rest:
                continue
            if "/" in rest:
                name = rest.split("/", 1)[0]
                agg = dirs.setdefault(name, {"count": 0, "size": 0})
                agg["count"] += 1
                agg["size"] += entry["size"]
            else:
                files_here.append({"name": rest, "path": path, "size": entry["size"]})

        self.table.setRowCount(0)
        self.breadcrumb_label.setText(f"📂 {self._current_dir}" if self._current_dir else "📂 / (корень)")
        self.btn_up.setEnabled(bool(self._current_dir))

        if self._current_dir:
            row = self.table.rowCount()
            self.table.insertRow(row)
            up_item = QTableWidgetItem("⬆ ..")
            up_item.setData(Qt.ItemDataRole.UserRole, {"type": "up"})
            self.table.setItem(row, 0, up_item)
            self.table.setItem(row, 1, QTableWidgetItem(""))

        for name in sorted(dirs):
            agg = dirs[name]
            row = self.table.rowCount()
            self.table.insertRow(row)
            full_path = f"{self._current_dir}/{name}" if self._current_dir else name
            item = QTableWidgetItem(f"📁 {name}/")
            item.setData(Qt.ItemDataRole.UserRole, {"type": "dir", "path": full_path})
            self.table.setItem(row, 0, item)
            count_word = "файл" if agg["count"] % 10 == 1 and agg["count"] % 100 != 11 else "файлов"
            self.table.setItem(row, 1, QTableWidgetItem(f"{_fmt_size(agg['size'])} ({agg['count']} {count_word})"))

        for f in sorted(files_here, key=lambda e: e["name"]):
            row = self.table.rowCount()
            self.table.insertRow(row)
            item = QTableWidgetItem(f"📄 {f['name']}")
            item.setData(Qt.ItemDataRole.UserRole, {"type": "file", "path": f["path"]})
            self.table.setItem(row, 0, item)
            size_item = QTableWidgetItem(_fmt_size(f["size"]))
            size_item.setData(Qt.ItemDataRole.UserRole, f["size"])
            self.table.setItem(row, 1, size_item)

        self._on_selection()

    def _navigate_into(self, path: str):
        self._current_dir = path
        self._render_current_dir()

    def _navigate_up(self):
        if not self._current_dir:
            return
        parent = self._current_dir.rsplit("/", 1)
        self._current_dir = parent[0] if len(parent) > 1 else ""
        self._render_current_dir()

    def _on_row_activated(self):
        entry = self._selected_entry()
        if not entry:
            return
        if entry["type"] == "up":
            self._navigate_up()
        elif entry["type"] == "dir":
            self._navigate_into(entry["path"])
        else:
            self._view_selected()

    # ── Selection ────────────────────────────────────────────────────────────

    def _selected_entry(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_selection(self):
        entry = self._selected_entry()
        is_file = entry is not None and entry["type"] == "file"
        self.btn_view.setEnabled(is_file)
        self.btn_delete.setEnabled(is_file)

    def _selected_path(self):
        entry = self._selected_entry()
        if entry and entry["type"] == "file":
            return entry["path"]
        return None


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
        build_id = self.mw.current_build_id()
        if not build_id:
            QMessageBox.warning(self, "Ошибка", "Выберите сборку вверху окна")
            return
        local_path, _ = QFileDialog.getOpenFileName(self, "Выберите файл для загрузки")
        if not local_path:
            return
        # Кладём в ТЕКУЩУЮ открытую папку (см. "Навигация по папкам" в
        # докстринге модуля) — раньше всегда только в корень сборки. Для
        # произвольного пути внутри дерева переименуйте/переместите
        # локально перед выбором либо используйте «🚀 Релизы» для обычной
        # публикации сборки — эта кнопка для отдельных файлов (readme,
        # poster.png и т.п.), не для чанков.
        name = Path(local_path).name
        rel_path = f"{self._current_dir}/{name}" if self._current_dir else name

        client = self._make_client(build_id)
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
