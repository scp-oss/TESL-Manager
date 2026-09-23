# ==================== main_window.py ====================
"""
Прямой запрос пользователя (2026-09-23, третий заход по этому GUI):
интерфейс был "сумбурный" — семь отдельных верхних вкладок вперемешку
(Релизы/Файлы сервера/Статик-папки/Депо (chunks)/Файлы на депо/
Настройки/Лог), два разных способа сделать почти одно и то же (старый
компонентный протокол ReleaseTab и новый chunk-протокол DepotTab, у
каждого свой набор WebDAV/Panel-полей). Переработано в боковое меню
из четырёх пунктов:

  🚀 Релизы              — DepotTab (chunk-протокол), компоненты+канал+публикация
  🗂️ Файлы на сервере    — FilesTab, переключается между WebDAV/Panel-браузерами
  ⚙️ Настройки            — WebDAV + Panel + backend-выбор + доп. параметры депо,
                            ЕДИНСТВЕННОЕ место, где это настраивается
  📝 Лог (расширенный)   — один центральный лог на всё приложение

"Статик-папки" убраны совсем (не нужны, static_folders_tab.py больше не
подключается). Старая вкладка "Релизы" (release_tab.py::ReleaseTab,
собственный протокол публикации на WebDAV) тоже больше не подключается —
её roль теперь играет DepotTab: "депо — это часть релизов, настраивается
в настройках либо WebDAV, либо токен с панели хранилища" (прямая
формулировка запроса). Модуль release_tab.py остаётся в репозитории
только ради класса ComponentRow — им пользуется и DepotTab.
"""
from pathlib import Path
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QFileDialog,
    QMessageBox, QGroupBox, QLineEdit, QCheckBox,
    QListWidget, QListWidgetItem, QStackedWidget, QComboBox, QSpinBox,
    QFormLayout, QStatusBar, QFrame, QScrollArea,
)
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtCore import pyqtSignal

from config import DEFAULT_COMPONENTS_CONFIG, COMPONENT_NAMES, get_window_title, LOG_FILE
from chunk_manager import DEFAULT_CHUNK_SIZE
from pack_writer import DEFAULT_PACK_SIZE
from file_selector import FileSelector
from manifest_manager import ManifestManager
from themes import ThemeManager
from theme_dialog import ThemeDialog
from depot_tab import DepotTab, CFG_KEY as DEPOT_CFG_KEY
from files_tab import FilesTab

CHUNK_SIZES = [
    ("512 KB  — максимальный delta",   512 * 1024),
    ("1 MB",                           1 * 1024 * 1024),
    ("4 MB   — рекомендуется",         4 * 1024 * 1024),
    ("8 MB",                           8 * 1024 * 1024),
    ("16 MB",                         16 * 1024 * 1024),
    ("32 MB  — крупные файлы",        32 * 1024 * 1024),
    ("64 MB  — минимальный overhead", 64 * 1024 * 1024),
]


class StatusBar(QFrame):
    """Компактная статус-панель."""

    folder_change_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFixedHeight(58)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(0)

        # ── Публикация (backend) ─────────────────────────────────────────────
        pub_info = QVBoxLayout()
        pub_info.setSpacing(1)
        lbl_pt = QLabel("Публикация")
        lbl_pt.setStyleSheet("font-size: 10px; color: #888;")
        self.lbl_backend = QLabel("не настроено")
        self.lbl_backend.setFont(QFont("Segoe UI", 9))
        self.lbl_backend.setMinimumWidth(260)
        pub_info.addWidget(lbl_pt)
        pub_info.addWidget(self.lbl_backend)
        layout.addLayout(pub_info)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setFixedWidth(1)
        sep1.setStyleSheet("background: #444; margin: 8px 16px;")
        layout.addSpacing(16)
        layout.addWidget(sep1)
        layout.addSpacing(16)

        # ── Компоненты ────────────────────────────────────────────────────────
        comp_info = QVBoxLayout()
        comp_info.setSpacing(1)
        lbl_ct = QLabel("Компоненты")
        lbl_ct.setStyleSheet("font-size: 10px; color: #888;")
        self.lbl_components = QLabel("не настроены")
        self.lbl_components.setFont(QFont("Segoe UI", 9))
        comp_info.addWidget(lbl_ct)
        comp_info.addWidget(self.lbl_components)
        layout.addLayout(comp_info)

        layout.addStretch()

    def set_backend(self, cfg: dict):
        backend = cfg.get("backend", "panel")
        if backend == "panel":
            panel = cfg.get("panel", {})
            base_url   = panel.get("base_url", "")
            build_name = panel.get("build_name", "")
            if base_url and build_name:
                from urllib.parse import urlparse
                host = urlparse(base_url).netloc or base_url
                self.lbl_backend.setText(f"TESL-Panel: {host} → {build_name}")
            else:
                self.lbl_backend.setText("TESL-Panel: не настроено")
        else:
            webdav = cfg.get("webdav", {})
            url = webdav.get("server_url", "")
            if url:
                from urllib.parse import urlparse
                host = urlparse(url).netloc
                self.lbl_backend.setText(f"WebDAV: {host} → {webdav.get('remote_path') or '/'}")
            else:
                self.lbl_backend.setText("WebDAV: не настроен")

    def set_components(self, components: dict):
        parts = []
        for name in COMPONENT_NAMES:
            c = components.get(name, {})
            if c.get("local_dir"):
                parts.append(f"✅ {name}")
            else:
                parts.append(f"⚠️ {name}")
        self.lbl_components.setText("  ".join(parts) if parts else "не настроены")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(get_window_title())
        self.resize(1150, 820)
        self.setMinimumSize(900, 620)

        self.theme_manager    = ThemeManager()
        self.file_selector    = FileSelector(self)
        self.config           = self.file_selector.config
        self.manifest_manager = ManifestManager(self.config)
        self._log_lines       = []   # полная (нефильтрованная) история лога, см. _build_log_tab()
        self._migrate_legacy_backend_config()

        self._create_ui()
        self.theme_manager.apply_theme(self)
        self._connect_signals()
        self._load_initial_data()

    # ── Migration: старые cfg["depot_tab"]["panel"/"webdav"/"depot"] → общие
    #    cfg["panel"]/cfg["backend"]/cfg["depot_publish"] (введены этим
    #    заходом) — только если новых ключей ещё нет, ничего не трогает у
    #    того, кто уже сохранил конфиг в новом виде. ────────────────────────────

    def _migrate_legacy_backend_config(self):
        cfg = self.config
        old = cfg.get("depot_tab", {})
        if old:
            if "backend" not in cfg and old.get("backend"):
                cfg["backend"] = old["backend"]
            if "panel" not in cfg and old.get("panel", {}).get("base_url"):
                cfg["panel"] = old["panel"]
            if not cfg.get("webdav", {}).get("server_url") and old.get("webdav", {}).get("server_url"):
                cfg["webdav"] = old["webdav"]
            if "depot_publish" not in cfg:
                d = old.get("depot", {})
                cfg["depot_publish"] = {
                    "use_packs":  old.get("use_packs", True),
                    "chunk_size": d.get("chunk_size", DEFAULT_CHUNK_SIZE),
                    "pack_size":  d.get("pack_size", DEFAULT_PACK_SIZE),
                }

        # Более поздняя миграция (2026-09-23, тем же вечером) — НЕ зависит
        # от "if old" выше (нужна и тем, у кого cfg["panel"] уже был
        # верхнеуровневым с этой же сессии, без старого cfg["depot_tab"]
        # вообще): cfg["panel"]
        # раньше хранил "project" — ИМЯ сборки как строку. TESL-Panel's
        # builds_db.py сделал id реальным ключом (прямой запрос
        # пользователя — имя не должно быть ключом связи между панелью и
        # менеджером) — старое имя нельзя надёжно превратить в id офлайн
        # (сетевого похода на панель здесь, при старте, делать не
        # хотим) — просто убираем его, чтобы не притвориться валидным
        # build_id и не словить непонятный 404. Пользователю нужно будет
        # заново выбрать сборку в комбобоксе на "Релизы" — один раз,
        # список подтянется тем же способом, что и раньше.
        panel = cfg.get("panel", {})
        if "project" in panel and "build_id" not in panel:
            panel = dict(panel)
            panel.pop("project", None)
            panel.setdefault("build_id", "")
            panel.setdefault("build_name", "")
            cfg["panel"] = panel
        # Больше не сохраняем backend/panel/depot внутри depot_tab — только
        # components/channel там теперь и нужны (см. depot_tab.py).
        for stale_key in ("backend", "panel", "webdav", "depot", "use_packs"):
            old.pop(stale_key, None)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _create_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        self._create_menu()

        self.status_panel = StatusBar()
        root.addWidget(self.status_panel)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(8)

        self.nav_list = QListWidget()
        self.nav_list.setFixedWidth(190)
        self.nav_list.setSpacing(2)
        self.nav_list.currentRowChanged.connect(self._on_nav_changed)
        body_layout.addWidget(self.nav_list)

        self.pages = QStackedWidget()
        body_layout.addWidget(self.pages, stretch=1)

        root.addWidget(body, stretch=1)

        self.depot_tab = DepotTab(self)
        self.depot_tab.log_message.connect(self.log_message)
        self._add_page("🚀 Релизы", self.depot_tab)

        self.files_tab = FilesTab(self)
        self.files_tab.log_message.connect(self.log_message)
        self._add_page("🗂️ Файлы на сервере", self.files_tab)

        self.settings_page_index = self._add_page("⚙️ Настройки", self._build_settings_tab())
        self._add_page("📝 Лог (расширенный)", self._build_log_tab())

        self.nav_list.setCurrentRow(0)

        self.sb = QStatusBar()
        self.setStatusBar(self.sb)
        self.sb.showMessage("Готов")

    def _add_page(self, label: str, widget: QWidget) -> int:
        self.nav_list.addItem(QListWidgetItem(label))
        return self.pages.addWidget(widget)

    def _on_nav_changed(self, row: int):
        if row >= 0:
            self.pages.setCurrentIndex(row)

    def show_settings_page(self):
        self.nav_list.setCurrentRow(self.settings_page_index)

    # ── Menu ──────────────────────────────────────────────────────────────────

    def _create_menu(self):
        mb = self.menuBar()

        fm = mb.addMenu("📁 Файл")
        a2 = QAction("Выход", self)
        a2.setShortcut("Ctrl+Q")
        a2.triggered.connect(self.close)
        fm.addAction(a2)

        sm = mb.addMenu("🌐 Сервер")
        a3 = QAction("Проверить соединение", self)
        a3.triggered.connect(self._test_connection)
        sm.addAction(a3)
        a4 = QAction("Обновить инфо о сборке", self)
        a4.triggered.connect(lambda: self.depot_tab._fetch_server_info())
        sm.addAction(a4)

        stm = mb.addMenu("⚙️ Настройки")
        a5 = QAction("Тема…", self)
        a5.triggered.connect(self.show_theme_dialog)
        stm.addAction(a5)

    # ── Settings page ────────────────────────────────────────────────────────
    # Единственное место в приложении, где настраивается: куда публиковать
    # (WebDAV/Panel), сами реквизиты обоих, и параметры депо (chunk size/
    # упаковка в pack-файлы) — прямой запрос пользователя ("депо — это
    # часть релизов, настраивается в настройках"). "Релизы" (DepotTab) и
    # "Файлы на сервере" (FilesTab) только ЧИТАЮТ эти ключи конфига.

    def _build_settings_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(10)

        # ── Куда публиковать ─────────────────────────────────────────────────
        backend_box = QGroupBox("Куда публиковать")
        backend_layout = QVBoxLayout(backend_box)
        self.backend_combo = QComboBox()
        self.backend_combo.addItems([
            "TESL-Panel (напрямую, минуя Nextcloud)",
            "WebDAV (Nextcloud)",
        ])
        self.backend_combo.currentIndexChanged.connect(self._save_backend_choice)
        backend_layout.addWidget(self.backend_combo)
        layout.addWidget(backend_box)

        # ── TESL-Panel ────────────────────────────────────────────────────────
        # Прямой запрос пользователя (2026-09-23): вместо URL+токена по
        # отдельности — одна строка ("код настройки"), сгенерированная
        # самой панелью (/admin/settings, см. её CLAUDE.md) и вставляемая
        # сюда целиком. decode_setup_code()/encode_setup_code() —
        # panel_client.py, формат зеркалит серверную сторону 1:1.
        panel_box = QGroupBox("TESL-Panel")
        panel_form = QFormLayout(panel_box)

        code_row = QHBoxLayout()
        self.panel_setup_code_edit = QLineEdit()
        self.panel_setup_code_edit.setPlaceholderText(
            "Вставьте код из панели: Настройки → Код настройки для TESL-Manager"
        )
        code_row.addWidget(self.panel_setup_code_edit, stretch=1)
        btn_connect_code = QPushButton("🔌 Подключить по коду")
        btn_connect_code.clicked.connect(self._connect_panel_by_code)
        code_row.addWidget(btn_connect_code)
        panel_form.addRow("Код настройки:", code_row)

        self.panel_connection_label = QLabel("Не подключено")
        self.panel_connection_label.setStyleSheet("color: #888; font-size: 9pt;")
        panel_form.addRow("", self.panel_connection_label)

        panel_hint = QLabel(
            "💡 Выбор сборки (какой проект на панели публиковать) — на странице «🚀 Релизы»."
        )
        panel_hint.setStyleSheet("color: #888; font-size: 9pt;")
        panel_form.addRow("", panel_hint)

        layout.addWidget(panel_box)

        # ── WebDAV ────────────────────────────────────────────────────────────
        wbox = QGroupBox("WebDAV (Nextcloud)")
        form = QFormLayout(wbox)
        form.setSpacing(8)

        self.webdav_url_edit      = QLineEdit()
        self.webdav_url_edit.setPlaceholderText("https://host/cloud/remote.php/dav/files/user")
        self.webdav_username_edit = QLineEdit()
        self.webdav_password_edit = QLineEdit()
        self.webdav_password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.webdav_path_edit     = QLineEdit()
        self.webdav_path_edit.setPlaceholderText("1TB/TESS/Instances/TESVAE")
        self.webdav_ssl_check     = QCheckBox("Проверять SSL сертификат")
        self.webdav_ssl_check.setChecked(True)

        form.addRow("URL сервера:",     self.webdav_url_edit)
        form.addRow("Пользователь:",    self.webdav_username_edit)
        form.addRow("Пароль:",          self.webdav_password_edit)
        form.addRow("Путь на сервере:", self.webdav_path_edit)
        form.addRow("",                 self.webdav_ssl_check)

        btn_row = QHBoxLayout()
        self.btn_save_webdav = QPushButton("💾 Сохранить")
        self.btn_save_webdav.setFixedHeight(36)
        self.btn_test_conn   = QPushButton("🔌 Проверить соединение")
        self.btn_test_conn.setFixedHeight(36)
        btn_row.addWidget(self.btn_save_webdav)
        btn_row.addWidget(self.btn_test_conn)
        btn_row.addStretch()
        form.addRow("", btn_row)

        layout.addWidget(wbox)

        # ── Дополнительно (депо) ─────────────────────────────────────────────
        adv_box = QGroupBox("Дополнительно (депо)")
        adv_form = QFormLayout(adv_box)

        self.chunk_combo = QComboBox()
        for label, _ in CHUNK_SIZES:
            self.chunk_combo.addItem(label)
        adv_form.addRow("Размер чанка:", self.chunk_combo)

        self.use_packs_check = QCheckBox(
            "Упаковывать чанки в pack-файлы (меньше отдельных файлов на диске сервера)"
        )
        self.use_packs_check.stateChanged.connect(
            lambda _: self.pack_size_spin.setEnabled(self.use_packs_check.isChecked())
        )
        adv_form.addRow("", self.use_packs_check)

        self.pack_size_spin = QSpinBox()
        self.pack_size_spin.setRange(16, 4096)
        self.pack_size_spin.setSuffix(" MB")
        adv_form.addRow("Размер pack-файла:", self.pack_size_spin)

        btn_save_adv = QPushButton("💾 Сохранить")
        btn_save_adv.clicked.connect(self._save_advanced_settings)
        adv_form.addRow("", btn_save_adv)

        layout.addWidget(adv_box)

        hint = QLabel(
            "💡 Компоненты сборки (Skyrim/MO2p/MO2ext) и канал (stable/beta/dev) "
            "настраиваются на странице «🚀 Релизы»."
        )
        hint.setStyleSheet("color: #888; font-size: 9pt; padding: 4px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addStretch()

        # Настроек здесь накопилось на больше одного экрана (куда
        # публиковать + Panel + WebDAV + доп. параметры депо) — без
        # прокрутки нижние блоки (WebDAV) визуально сжимались/налезали
        # друг на друга на обычной высоте окна, ровно то, на что жаловался
        # пользователь ("почини скукуренные строки"). QScrollArea вместо
        # попытки уместить всё без прокрутки.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(tab)
        return scroll

    # ── Log page — единый центральный лог ────────────────────────────────────

    def _build_log_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Фильтр:"))
        self.log_filter_edit = QLineEdit()
        self.log_filter_edit.setPlaceholderText("Показывать только строки, содержащие…")
        self.log_filter_edit.textChanged.connect(self._apply_log_filter)
        filter_row.addWidget(self.log_filter_edit)
        layout.addLayout(filter_row)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 9))

        btn_row = QHBoxLayout()
        btn_clear = QPushButton("🗑 Очистить")
        btn_save  = QPushButton("💾 Сохранить")
        btn_clear.setFixedHeight(30)
        btn_save.setFixedHeight(30)
        btn_clear.clicked.connect(self._clear_log)
        btn_save.clicked.connect(self._save_log)
        btn_row.addWidget(btn_clear)
        btn_row.addWidget(btn_save)
        btn_row.addStretch()

        layout.addWidget(self.log_text)
        layout.addLayout(btn_row)
        return tab

    def _apply_log_filter(self, _needle: str):
        self._refresh_log_display()

    def _refresh_log_display(self):
        needle = self.log_filter_edit.text().strip().lower()
        lines = self._log_lines if not needle else [
            l for l in self._log_lines if needle in l.lower()
        ]
        self.log_text.setPlainText("\n".join(lines))
        self.log_text.verticalScrollBar().setValue(
            self.log_text.verticalScrollBar().maximum()
        )

    def _clear_log(self):
        self._log_lines = []
        self.log_text.clear()

    # ── Signals ───────────────────────────────────────────────────────────────

    def _connect_signals(self):
        self.btn_save_webdav.clicked.connect(self.save_webdav_settings)
        self.btn_test_conn.clicked.connect(self._test_connection)
        self.file_selector.config_updated.connect(self._on_config_updated)

    # ── Initial data ──────────────────────────────────────────────────────────

    def _load_initial_data(self):
        cfg = self.file_selector.config

        if "components" not in cfg:
            cfg["components"] = {}
        for name in COMPONENT_NAMES:
            if name not in cfg["components"]:
                cfg["components"][name] = dict(DEFAULT_COMPONENTS_CONFIG[name])

        # Backend choice
        self.backend_combo.blockSignals(True)
        self.backend_combo.setCurrentIndex(0 if cfg.get("backend", "panel") == "panel" else 1)
        self.backend_combo.blockSignals(False)

        # Panel — если уже подключено раньше (base_url+token в cfg), поле
        # кода не должно выглядеть пустым: перекодируем сохранённые
        # значения обратно в код настройки (см. panel_client.
        # encode_setup_code()), тот же принцип, что и в TESL-Panel самой
        # (код детерминирован от url+token, генерировать его заново на
        # сервере/клиенте — одно и то же).
        panel = cfg.get("panel", {})
        base_url = panel.get("base_url", "")
        token    = panel.get("token", "")
        if base_url and token:
            from panel_client import encode_setup_code
            self.panel_setup_code_edit.setText(encode_setup_code(base_url, token))
            self._update_panel_connection_label(base_url)

        # WebDAV
        webdav = self.file_selector.get_webdav_config()
        if webdav.get("server_url"):
            self.webdav_url_edit.setText(webdav.get("server_url", ""))
            self.webdav_username_edit.setText(webdav.get("username", ""))
            self.webdav_password_edit.setText(webdav.get("password", ""))
            self.webdav_path_edit.setText(webdav.get("remote_path", ""))
            self.webdav_ssl_check.setChecked(webdav.get("verify_ssl", True))

        # Advanced / depot publish
        publish = cfg.get("depot_publish", {})
        self.use_packs_check.setChecked(publish.get("use_packs", True))
        self.pack_size_spin.setValue(publish.get("pack_size", DEFAULT_PACK_SIZE) // 1024 // 1024)
        self.pack_size_spin.setEnabled(self.use_packs_check.isChecked())
        target_size = publish.get("chunk_size", DEFAULT_CHUNK_SIZE)
        for i, (_, size) in enumerate(CHUNK_SIZES):
            if size == target_size:
                self.chunk_combo.setCurrentIndex(i)
                break

        self.status_panel.set_backend(cfg)
        self.status_panel.set_components(cfg.get(DEPOT_CFG_KEY, {}).get("components", {}))

    # ── Backend settings handlers ────────────────────────────────────────────

    def _save_backend_choice(self, _idx: int = 0):
        cfg = self.config
        cfg["backend"] = "panel" if self.backend_combo.currentIndex() == 0 else "webdav"
        self.file_selector.save_config()
        self.status_panel.set_backend(cfg)
        self.log_message(f"✅ Backend публикации: {cfg['backend']}")

    def _update_panel_connection_label(self, base_url: str):
        from urllib.parse import urlparse
        host = urlparse(base_url).netloc or base_url
        self.panel_connection_label.setText(f"✅ Подключено: {host}")
        self.panel_connection_label.setStyleSheet("color: #4caf6b; font-size: 9pt;")

    def _connect_panel_by_code(self):
        """Разбирает код настройки (см. panel_client.decode_setup_code(),
        сгенерирован на TESL-Panel: Настройки → Код настройки) и сохраняет
        base_url/token — прямой запрос пользователя: одна строка вместо
        URL+токена по отдельности, ничего кроме этого поля для авторизации
        вводить не нужно."""
        from panel_client import decode_setup_code
        decoded = decode_setup_code(self.panel_setup_code_edit.text())
        if decoded is None:
            QMessageBox.warning(
                self, "Ошибка",
                "Не удалось разобрать код настройки — проверьте, что он скопирован "
                "полностью (Настройки → Код настройки в TESL-Panel).",
            )
            return

        cfg = self.config
        old_base_url = cfg.get("panel", {}).get("base_url", "")
        panel_cfg = {
            "base_url":   decoded["base_url"],
            "token":      decoded["token"],
            "build_id":   cfg.get("panel", {}).get("build_id", ""),
            "build_name": cfg.get("panel", {}).get("build_name", ""),
            "verify_ssl": True,
        }
        # Другой адрес панели — старая выбранная сборка почти наверняка
        # относится к другой панели (id из одной БД builds.db бессмыслен
        # для другой), список нужно перезагрузить с нуля, а не молча
        # оставлять невалидный выбор. Сам combo выбора сборки живёт
        # теперь на странице "Релизы" (DepotTab.build_combo), не здесь —
        # см. её же _refresh_builds()/_create_new_build().
        if decoded["base_url"] != old_base_url:
            panel_cfg["build_id"]   = ""
            panel_cfg["build_name"] = ""
            self.depot_tab.build_combo.clear()

        cfg["panel"] = panel_cfg
        self.file_selector.save_config()
        self._update_panel_connection_label(decoded["base_url"])
        self.status_panel.set_backend(cfg)
        self.log_message(f"✅ Подключено к панели: {decoded['base_url']}")
        self.depot_tab._refresh_builds()

    def _save_advanced_settings(self):
        cfg = self.config
        cfg["depot_publish"] = {
            "use_packs":  self.use_packs_check.isChecked(),
            "chunk_size": CHUNK_SIZES[self.chunk_combo.currentIndex()][1],
            "pack_size":  self.pack_size_spin.value() * 1024 * 1024,
        }
        self.file_selector.save_config()
        self.log_message("✅ Дополнительные настройки депо сохранены")

    # ── WebDAV ────────────────────────────────────────────────────────────────

    def save_webdav_settings(self):
        url      = self.webdav_url_edit.text().strip()
        username = self.webdav_username_edit.text().strip()
        password = self.webdav_password_edit.text()
        path     = self.webdav_path_edit.text().strip()
        ssl      = self.webdav_ssl_check.isChecked()

        if not url:
            QMessageBox.warning(self, "Ошибка", "Введите URL сервера!")
            return
        if not username:
            QMessageBox.warning(self, "Ошибка", "Введите имя пользователя!")
            return

        ok = self.file_selector.set_webdav_config(url, username, password, path, ssl)
        if ok:
            self.status_panel.set_backend(self.config)
            self.log_message("✅ Настройки WebDAV сохранены")
            QMessageBox.information(self, "Сохранено", "Настройки WebDAV сохранены!")
        else:
            QMessageBox.critical(self, "Ошибка", "Не удалось сохранить настройки")

    def _test_connection(self):
        cfg = self.file_selector.config
        backend = cfg.get("backend", "panel")
        if backend == "webdav" and not cfg.get("webdav", {}).get("server_url"):
            QMessageBox.warning(self, "Ошибка", "Сначала настройте WebDAV!")
            self.show_settings_page()
            return
        if backend == "panel" and not cfg.get("panel", {}).get("base_url"):
            QMessageBox.warning(self, "Ошибка", "Сначала настройте TESL-Panel!")
            self.show_settings_page()
            return
        self.depot_tab._test_connection()

    # ── Config updated ────────────────────────────────────────────────────────

    def _on_config_updated(self, config: dict):
        self.status_panel.set_backend(config)
        self.status_panel.set_components(config.get(DEPOT_CFG_KEY, {}).get("components", {}))

    # ── Log ───────────────────────────────────────────────────────────────────

    def log_message(self, msg: str):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        self._log_lines.append(line)
        # Пишем на диск СРАЗУ (append+flush), не только в память — до этой
        # правки лог существовал только в _log_lines/log_text и терялся
        # целиком при любом крэше приложения (единственный способ сохранить
        # его — кнопка "Сохранить лог", нажать которую после неожиданного
        # крэша уже не получится). config.LOG_FILE был объявлен, но нигде
        # не использовался — живой случай (2026-09-23): публикация большой
        # сборки (Skyrim, хэширование ~170К файлов) уронила приложение без
        # единого следа, потому что .exe собран с --noconsole --windowed
        # (см. .github/workflows/build-release.yml) — ни консоли, ни
        # сохранённого лога не было куда посмотреть. См. также sys.excepthook
        # в main.py — эта же логика ловит и падения ВНЕ обработанных try/except.
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass  # диск недоступен/только для чтения — не мешаем работе GUI
        # Фильтр применяем и здесь — новая строка сразу учитывает текущий
        # текст фильтра, а не только при следующем его изменении.
        needle = self.log_filter_edit.text().strip().lower()
        if not needle or needle in self._log_lines[-1].lower():
            self.log_text.append(self._log_lines[-1])
            self.log_text.verticalScrollBar().setValue(
                self.log_text.verticalScrollBar().maximum()
            )

    def _save_log(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить лог",
            str(Path.home() / "uploder_log.txt"),
            "Текстовые файлы (*.txt)"
        )
        if path:
            try:
                Path(path).write_text("\n".join(self._log_lines), encoding="utf-8")
                self.log_message(f"✅ Лог → {path}")
            except Exception as e:
                QMessageBox.critical(self, "Ошибка", str(e))

    # ── Theme ─────────────────────────────────────────────────────────────────

    def show_theme_dialog(self):
        dlg = ThemeDialog(self, self.theme_manager.get_current_theme())
        dlg.theme_changed.connect(self._change_theme)
        dlg.exec()

    def _change_theme(self, name: str):
        if self.theme_manager.set_theme(name):
            self.theme_manager.apply_theme(self)

    # ── Close ─────────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        self.file_selector.save_config()
        event.accept()
