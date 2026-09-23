# ==================== server_files_tab.py ====================
"""
Вкладка просмотра файлов на WebDAV сервере.
Показывает дерево папок и файлов с размерами и датами.
"""
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTreeWidget, QTreeWidgetItem, QProgressBar, QLineEdit,
    QMessageBox, QHeaderView, QAbstractItemView, QMenu,
    QGroupBox, QSplitter,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QFont, QColor, QCursor

DAV_NS = "{DAV:}"


def _fmt_size(n: int) -> str:
    if n < 0:
        return "—"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_dt(s: str) -> str:
    # WebDAV lastmodified: "Fri, 18 Apr 2026 15:30:00 GMT"
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return s[:16] if s else "—"


# ── Worker ────────────────────────────────────────────────────────────────────

class FetchWorker(QObject):
    """Рекурсивный листинг директории через PROPFIND Depth: infinity"""
    finished = pyqtSignal(list)   # list of dicts
    error    = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, config: dict, remote_path: str):
        super().__init__()
        self.config      = config
        self.remote_path = remote_path

    def run(self):
        try:
            from depot_sync_manager import NextcloudDAV
            cfg = self.config.get("webdav", {})
            dav = NextcloudDAV(
                cfg.get("server_url", ""),
                cfg.get("username", ""),
                cfg.get("password", ""),
                cfg.get("verify_ssl", True),
            )

            self.progress.emit("Подключение…")
            ok, msg = dav.test_connection()
            if not ok:
                self.error.emit(f"Ошибка подключения: {msg}")
                dav.close()
                return

            self.progress.emit(f"Сканируем {self.remote_path}…")

            # PROPFIND Depth: infinity — проверено probe, работает
            import requests
            r = dav.session.request(
                "PROPFIND",
                dav.url(self.remote_path),
                headers={
                    "Depth": "infinity",
                    "Content-Type": "application/xml",
                },
                data=b"""<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:getcontentlength/>
    <D:getlastmodified/>
    <D:resourcetype/>
    <D:displayname/>
  </D:prop>
</D:propfind>""",
                timeout=60,
            )

            if r.status_code not in (200, 207):
                self.error.emit(f"PROPFIND вернул HTTP {r.status_code}")
                dav.close()
                return

            self.progress.emit("Разбираем список файлов…")
            items = self._parse_propfind(r.content, dav, self.remote_path)
            dav.close()
            self.finished.emit(items)

        except Exception as e:
            import traceback
            self.error.emit(f"{e}\n{traceback.format_exc()}")

    def _parse_propfind(self, content: bytes, dav, base_path: str) -> list:
        root  = ET.fromstring(content)
        items = []

        for resp in root.findall(f"{DAV_NS}response"):
            href_el = resp.find(f"{DAV_NS}href")
            if href_el is None or not href_el.text:
                continue

            href    = urllib.parse.unquote(href_el.text)
            rel     = dav._href_to_rel(href_el.text, base_path)
            # base_path сам по себе тоже возвращается — пропускаем
            if rel is None:
                continue

            # Тип: директория или файл
            is_dir = False
            for propstat in resp.findall(f"{DAV_NS}propstat"):
                if propstat.find(f".//{DAV_NS}resourcetype/{DAV_NS}collection") is not None:
                    is_dir = True
                    break

            # Размер
            size = -1
            size_el = resp.find(f".//{DAV_NS}getcontentlength")
            if size_el is not None and size_el.text:
                try:
                    size = int(size_el.text)
                except ValueError:
                    pass

            # Дата
            date = ""
            date_el = resp.find(f".//{DAV_NS}getlastmodified")
            if date_el is not None and date_el.text:
                date = date_el.text

            items.append({
                "path":   rel,
                "is_dir": is_dir,
                "size":   size,
                "date":   date,
            })

        return items


# ── ServerFilesTab ────────────────────────────────────────────────────────────

class ServerFilesTab(QWidget):
    log_message = pyqtSignal(str)

    def __init__(self, main_window):
        super().__init__()
        self.mw      = main_window
        self._worker = None
        self._thread = None
        self._all_items: list = []
        self._init_ui()

    def _init_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ── Тулбар ───────────────────────────────────────────────────────────
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self.btn_refresh = QPushButton("🔄 Обновить список")
        self.btn_refresh.setFixedHeight(34)
        self.btn_refresh.clicked.connect(self._refresh)
        toolbar.addWidget(self.btn_refresh)

        toolbar.addSpacing(8)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Поиск по имени файла…")
        self.search_edit.setFixedHeight(34)
        self.search_edit.textChanged.connect(self._filter_tree)
        toolbar.addWidget(self.search_edit, stretch=1)

        self.btn_stop = QPushButton("⏹ Стоп")
        self.btn_stop.setFixedHeight(34)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        toolbar.addWidget(self.btn_stop)

        root.addLayout(toolbar)

        # ── Статистика ────────────────────────────────────────────────────────
        self.stat_label = QLabel("Нажмите «Обновить список» для загрузки")
        self.stat_label.setStyleSheet("font-size: 10px;")
        root.addWidget(self.stat_label)

        # ── Прогресс ─────────────────────────────────────────────────────────
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFixedHeight(12)
        self.progress_bar.hide()
        root.addWidget(self.progress_bar)

        # ── Дерево файлов ─────────────────────────────────────────────────────
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Путь", "Размер", "Дата изменения"])
        self.tree.setColumnWidth(0, 560)
        self.tree.setColumnWidth(1, 90)
        self.tree.setColumnWidth(2, 140)
        hh = self.tree.header()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.setFont(QFont("Consolas", 9))
        root.addWidget(self.tree)

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh(self):
        cfg = self.mw.file_selector.config
        if not cfg.get("webdav", {}).get("server_url"):
            QMessageBox.warning(self, "Ошибка", "Настройте WebDAV в настройках!")
            self.mw.show_settings_page()
            return

        remote_path = cfg.get("webdav", {}).get("remote_path", "")
        if not remote_path:
            QMessageBox.warning(self, "Ошибка", "Укажите путь на сервере в настройках!")
            return

        self._set_busy(True)
        self.tree.clear()
        self._all_items = []
        self.stat_label.setText(f"Загружаем {remote_path}…")

        self._worker = FetchWorker(cfg, remote_path)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)
        self._worker.progress.connect(self.stat_label.setText)
        self._worker.finished.connect(self._on_loaded)
        self._worker.error.connect(self._on_error)
        self._thread.started.connect(self._worker.run)
        self._thread.start()

    def _stop(self):
        # FetchWorker не имеет stop — просто прерываем поток
        if self._thread and self._thread.isRunning():
            self._thread.quit()
        self._set_busy(False)
        self.stat_label.setText("Остановлено")

    # ── Load result ───────────────────────────────────────────────────────────

    def _on_loaded(self, items: list):
        self._cleanup_thread()
        self._set_busy(False)
        self._all_items = items
        self._build_tree(items)

        files = [i for i in items if not i["is_dir"]]
        dirs  = [i for i in items if i["is_dir"]]
        total_size = sum(i["size"] for i in files if i["size"] >= 0)
        self.stat_label.setText(
            f"Папок: {len(dirs)}   Файлов: {len(files)}   "
            f"Общий размер: {_fmt_size(total_size)}"
        )
        self.log_message.emit(
            f"✅ Сервер: {len(files)} файлов, {_fmt_size(total_size)}"
        )

    def _on_error(self, msg: str):
        self._cleanup_thread()
        self._set_busy(False)
        self.stat_label.setText(f"Ошибка: {msg[:80]}")
        self.log_message.emit(f"❌ {msg}")
        QMessageBox.warning(self, "Ошибка загрузки", msg[:300])

    # ── Tree builder ──────────────────────────────────────────────────────────

    def _build_tree(self, items: list):
        """Строим иерархическое дерево из плоского списка путей."""
        self.tree.clear()
        node_map: dict[str, QTreeWidgetItem] = {}   # path → item

        # Сортируем: сначала папки, потом файлы, алфавитно
        sorted_items = sorted(
            items,
            key=lambda x: (not x["is_dir"], x["path"].lower())
        )

        for entry in sorted_items:
            path   = entry["path"]
            is_dir = entry["is_dir"]
            parts  = path.split("/")
            name   = parts[-1] or parts[-2]  # имя последнего сегмента

            # Ищем родителя
            parent_path = "/".join(parts[:-1]) if len(parts) > 1 else ""
            parent_item = node_map.get(parent_path)

            if parent_item:
                item = QTreeWidgetItem(parent_item)
            else:
                item = QTreeWidgetItem(self.tree)

            # Иконка + имя
            icon = "📁 " if is_dir else "📄 "
            item.setText(0, icon + name)
            item.setData(0, Qt.ItemDataRole.UserRole, path)

            # Размер
            if not is_dir and entry["size"] >= 0:
                item.setText(1, _fmt_size(entry["size"]))
                item.setData(1, Qt.ItemDataRole.UserRole, entry["size"])
            else:
                item.setText(1, "—")

            # Дата
            item.setText(2, _fmt_dt(entry["date"]))

            # Цвет для директорий
            if is_dir:
                item.setForeground(0, QColor("#569cd6"))

            node_map[path] = item

        self.tree.expandToDepth(1)

    # ── Filter ────────────────────────────────────────────────────────────────

    def _filter_tree(self, text: str):
        """Показываем только элементы содержащие текст поиска."""
        text = text.strip().lower()

        def _set_visible(item: QTreeWidgetItem, visible: bool):
            item.setHidden(not visible)
            for i in range(item.childCount()):
                _set_visible(item.child(i), visible)

        def _match(item: QTreeWidgetItem) -> bool:
            name = item.text(0).lower()
            if text in name:
                return True
            for i in range(item.childCount()):
                if _match(item.child(i)):
                    return True
            return False

        def _apply(item: QTreeWidgetItem):
            if not text:
                item.setHidden(False)
                for i in range(item.childCount()):
                    _apply(item.child(i))
                return
            has_match = _match(item)
            item.setHidden(not has_match)
            if has_match:
                item.setExpanded(True)
            for i in range(item.childCount()):
                _apply(item.child(i))

        root = self.tree.invisibleRootItem()
        for i in range(root.childCount()):
            _apply(root.child(i))

    # ── Context menu ──────────────────────────────────────────────────────────

    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item:
            return

        path = item.data(0, Qt.ItemDataRole.UserRole)
        menu = QMenu(self)

        act_copy = menu.addAction("📋 Копировать путь")
        act_copy.triggered.connect(lambda: self._copy_path(path))

        menu.addSeparator()

        act_refresh = menu.addAction("🔄 Обновить список")
        act_refresh.triggered.connect(self._refresh)

        menu.exec(QCursor.pos())

    def _copy_path(self, path: str):
        from PyQt6.QtWidgets import QApplication
        QApplication.clipboard().setText(path)
        self.log_message.emit(f"📋 Скопировано: {path}")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool):
        self.btn_refresh.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)
        self.progress_bar.setVisible(busy)

    def _cleanup_thread(self):
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(2000)
        self._worker = None
        self._thread = None
