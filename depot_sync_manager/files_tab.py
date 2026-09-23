# ==================== files_tab.py ====================
"""
Страница "🗂️ Файлы на сервере" — тонкая обёртка, показывающая ОДИН из
двух уже существующих файловых браузеров в зависимости от того, какой
backend публикации сейчас выбран на странице "⚙️ Настройки"
(cfg["backend"] — "panel" | "webdav"):

  - "panel"  → DepotFilesTab (depot_files_tab.py) — Bearer JSON-API
               TESL-Panel, десктоп-эквивалент её /admin/.../files.
  - "webdav" → ServerFilesTab (server_files_tab.py) — PROPFIND-дерево
               Nextcloud.

Оба виджета не изменены сами по себе — просто оба созданы и лежат в
QStackedWidget, между ними переключаемся при каждом показе страницы
(showEvent), а не по отдельной вкладке, per прямой запрос пользователя
"депо это часть релизов, настраивается в настройках либо вебдав либо
токен с панели хранилища" — из этого следует, что и файловый браузер
должен быть один пункт бокового меню, а не два отдельных.
"""
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QStackedWidget
from PyQt6.QtCore import pyqtSignal


class FilesTab(QWidget):
    log_message = pyqtSignal(str)

    def __init__(self, main_window):
        super().__init__()
        self.mw = main_window

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.info_label = QLabel()
        self.info_label.setStyleSheet("color: #888; font-size: 9pt; padding: 2px 4px;")
        layout.addWidget(self.info_label)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack)

        from server_files_tab import ServerFilesTab
        from depot_files_tab import DepotFilesTab

        self.webdav_widget = ServerFilesTab(main_window)
        self.panel_widget  = DepotFilesTab(main_window)
        self.webdav_widget.log_message.connect(self.log_message)
        self.panel_widget.log_message.connect(self.log_message)

        self._idx_webdav = self.stack.addWidget(self.webdav_widget)
        self._idx_panel  = self.stack.addWidget(self.panel_widget)

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_backend()

    def _sync_backend(self):
        backend = self.mw.file_selector.config.get("backend", "panel")
        if backend == "panel":
            self.stack.setCurrentIndex(self._idx_panel)
            self.info_label.setText(
                "Бэкенд: TESL-Panel — изменить на странице «⚙️ Настройки»"
            )
        else:
            self.stack.setCurrentIndex(self._idx_webdav)
            self.info_label.setText(
                "Бэкенд: WebDAV — изменить на странице «⚙️ Настройки»"
            )
