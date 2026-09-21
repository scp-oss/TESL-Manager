# ==================== themes.py ====================
from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QPalette, QColor
from PyQt6.QtWidgets import QApplication
import sys


class ThemeManager:
    THEMES = {
        "system": "Системная",
        "light":  "Светлая",
        "dark":   "Тёмная"
    }

    def __init__(self):
        self.settings     = QSettings("Uploder", "Theme")
        self.current_theme = self.settings.value("theme", "system", type=str)

    def get_current_theme(self) -> str:
        return self.current_theme

    def set_theme(self, name: str) -> bool:
        if name in self.THEMES:
            self.current_theme = name
            self.settings.setValue("theme", name)
            return True
        return False

    # ── System theme detection ────────────────────────────────────────────────

    def detect_system_theme(self) -> str:
        """
        Определяет тему ОС.
        Порядок попыток:
          1. darkdetect (pip install darkdetect) — кросс-платформенно
          2. Windows registry — AppsUseLightTheme
          3. Qt palette heuristic — если фон темнее 128 → тёмная
        """
        # 1. darkdetect
        try:
            import darkdetect
            result = darkdetect.theme()          # "Dark" | "Light" | None
            if result:
                return "dark" if result.lower() == "dark" else "light"
        except Exception:
            pass

        # 2. Windows registry (работает без сторонних пакетов)
        if sys.platform == "win32":
            try:
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
                )
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                winreg.CloseKey(key)
                # 0 = тёмная, 1 = светлая
                return "light" if value == 1 else "dark"
            except Exception:
                pass

        # 3. Qt palette heuristic
        try:
            app = QApplication.instance()
            if app:
                bg = app.palette().color(QPalette.ColorRole.Window)
                return "dark" if bg.lightness() < 128 else "light"
        except Exception:
            pass

        return "light"

    # ── Apply ─────────────────────────────────────────────────────────────────

    def apply_theme(self, widget=None):
        theme = self.current_theme
        if theme == "system":
            theme = self.detect_system_theme()

        stylesheet = self.get_stylesheet(theme)

        if widget:
            widget.setStyleSheet(stylesheet)
            if isinstance(widget, QApplication):
                palette = self.get_palette(theme)
                if palette:
                    widget.setPalette(palette)

        return stylesheet

    # ── Palette ───────────────────────────────────────────────────────────────

    def get_palette(self, theme_name: str) -> QPalette:
        palette = QPalette()
        if theme_name == "dark":
            c = {
                QPalette.ColorRole.Window:          QColor(45, 45, 48),
                QPalette.ColorRole.WindowText:      QColor(241, 241, 241),
                QPalette.ColorRole.Base:            QColor(28, 28, 30),
                QPalette.ColorRole.AlternateBase:   QColor(40, 40, 43),
                QPalette.ColorRole.Text:            QColor(241, 241, 241),
                QPalette.ColorRole.Button:          QColor(50, 50, 53),
                QPalette.ColorRole.ButtonText:      QColor(241, 241, 241),
                QPalette.ColorRole.Highlight:       QColor(42, 130, 218),
                QPalette.ColorRole.HighlightedText: QColor(255, 255, 255),
                QPalette.ColorRole.Link:            QColor(42, 130, 218),
                QPalette.ColorRole.ToolTipBase:     QColor(50, 50, 53),
                QPalette.ColorRole.ToolTipText:     QColor(241, 241, 241),
                QPalette.ColorRole.PlaceholderText: QColor(120, 120, 120),
            }
            for role, color in c.items():
                palette.setColor(role, color)
        return palette

    # ── Stylesheets ───────────────────────────────────────────────────────────

    def get_stylesheet(self, theme_name: str) -> str:
        if theme_name == "dark":
            return self._dark()
        return self._light()

    # ─────────────────────────────────────────────────────────────────────────
    # LIGHT
    # ─────────────────────────────────────────────────────────────────────────
    def _light(self) -> str:
        return """
        QMainWindow, QDialog { background: #f0f0f0; }

        QGroupBox {
            font-weight: bold; font-size: 10pt;
            border: 1px solid #c8c8c8; border-radius: 6px;
            margin-top: 10px; padding-top: 10px;
            background: #ffffff; color: #222;
        }
        QGroupBox::title {
            subcontrol-origin: margin; subcontrol-position: top left;
            left: 10px; padding: 0 6px; color: #1a1a2e;
        }

        QLabel { color: #222; font-size: 10pt; }

        QLineEdit, QTextEdit, QPlainTextEdit {
            background: #fff; color: #111;
            border: 1px solid #bbb; border-radius: 5px;
            padding: 5px 8px; font-size: 10pt;
            selection-background-color: #0078d4; selection-color: #fff;
        }
        QLineEdit:focus, QTextEdit:focus { border: 1px solid #0078d4; }
        QLineEdit:read-only { background: #f5f5f5; color: #555; }

        QListWidget, QTableWidget {
            background: #fff; color: #111;
            border: 1px solid #bbb; border-radius: 5px;
            font-size: 10pt; gridline-color: #e0e0e0;
        }
        QListWidget::item { padding: 5px 8px; border-bottom: 1px solid #eee; }
        QListWidget::item:selected, QTableWidget::item:selected {
            background: #0078d4; color: #fff;
        }
        QListWidget::item:hover { background: #e8f0fe; }
        QTableWidget::item { padding: 4px 8px; }
        QTableWidget::item:alternate { background: #f7f7f7; }
        QHeaderView::section {
            background: #e8e8e8; color: #333;
            border: none; border-right: 1px solid #ccc;
            border-bottom: 1px solid #ccc;
            padding: 5px 8px; font-weight: bold;
        }

        QPushButton {
            background: #0078d4; color: #fff;
            border: none; border-radius: 5px;
            padding: 6px 16px; font-size: 10pt; font-weight: 600;
            min-height: 32px;
        }
        QPushButton:hover    { background: #106ebe; }
        QPushButton:pressed  { background: #005a9e; }
        QPushButton:disabled { background: #bbb; color: #888; }

        QPushButton[flat="true"] {
            background: transparent; color: #0078d4; border: 1px solid #0078d4;
        }
        QPushButton[flat="true"]:hover { background: #e8f0fe; }

        QComboBox {
            background: #fff; color: #111;
            border: 1px solid #bbb; border-radius: 5px;
            padding: 4px 8px; font-size: 10pt; min-height: 28px;
        }
        QComboBox:focus { border: 1px solid #0078d4; }
        QComboBox::drop-down { border: none; width: 20px; }
        QComboBox QAbstractItemView {
            background: #fff; color: #111;
            border: 1px solid #bbb; selection-background-color: #0078d4;
        }

        QCheckBox { color: #222; font-size: 10pt; }
        QCheckBox::indicator { width: 16px; height: 16px; }

        QTabWidget::pane {
            border: 1px solid #c8c8c8; border-radius: 6px;
            background: #fff; padding: 4px;
        }
        QTabBar::tab {
            background: #e8e8e8; color: #555;
            padding: 8px 20px; margin-right: 2px;
            border: 1px solid #ccc; border-bottom: none;
            border-top-left-radius: 5px; border-top-right-radius: 5px;
            font-size: 10pt;
        }
        QTabBar::tab:selected {
            background: #fff; color: #0078d4; border-color: #0078d4;
            font-weight: bold;
        }
        QTabBar::tab:hover:!selected { background: #f0f0f0; }

        QProgressBar {
            border: 1px solid #bbb; border-radius: 5px;
            text-align: center; color: #333;
            background: #e8e8e8; min-height: 16px;
        }
        QProgressBar::chunk { background: #0078d4; border-radius: 4px; }

        QScrollBar:vertical {
            background: #f0f0f0; width: 10px; border-radius: 5px;
        }
        QScrollBar::handle:vertical {
            background: #bbb; border-radius: 5px; min-height: 30px;
        }
        QScrollBar::handle:vertical:hover { background: #999; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

        QStatusBar { background: #121212; color: #eee; font-size: 9pt; }
        QMenuBar { background: #121212; color: #eee; }
        QMenuBar::item:selected { background: #3d3d3d; }
        QMenu { background: #fff; color: #111; border: 1px solid #ccc; }
        QMenu::item:selected { background: #0078d4; color: #fff; }

        QSplitter::handle { background: #ddd; }
        QFrame[frameShape="4"] { color: #ccc; }  /* VLine */
        QFrame[frameShape="5"] { color: #ccc; }  /* HLine */
        """

    # ─────────────────────────────────────────────────────────────────────────
    # DARK
    # ─────────────────────────────────────────────────────────────────────────
    def _dark(self) -> str:
        return """
        QMainWindow, QDialog { background: #1e1e1e; }
        QWidget { background: #1e1e1e; color: #f1f1f1; }

        QGroupBox {
            font-weight: bold; font-size: 10pt;
            border: 1px solid #3a3a3a; border-radius: 6px;
            margin-top: 10px; padding-top: 10px;
            background: #252526; color: #ddd;
        }
        QGroupBox::title {
            subcontrol-origin: margin; subcontrol-position: top left;
            left: 10px; padding: 0 6px; color: #aaa;
        }

        QLabel { color: #ddd; font-size: 10pt; }

        QLineEdit, QTextEdit, QPlainTextEdit {
            background: #121212; color: #f1f1f1;
            border: 1px solid #3f3f3f; border-radius: 5px;
            padding: 5px 8px; font-size: 10pt;
            selection-background-color: #264f78; selection-color: #fff;
        }
        QLineEdit:focus, QTextEdit:focus { border: 1px solid #569cd6; }
        QLineEdit:read-only { background: #252526; color: #888; }

        QListWidget, QTableWidget {
            background: #252526; color: #f1f1f1;
            border: 1px solid #3a3a3a; border-radius: 5px;
            font-size: 10pt; gridline-color: #333;
        }
        QListWidget::item { padding: 5px 8px; border-bottom: 1px solid #333; }
        QListWidget::item:selected, QTableWidget::item:selected {
            background: #264f78; color: #fff;
        }
        QListWidget::item:hover { background: #2a2d2e; }
        QTableWidget::item { padding: 4px 8px; color: #f1f1f1; }
        QTableWidget::item:alternate { background: #2a2a2a; }
        QHeaderView::section {
            background: #333; color: #ccc;
            border: none; border-right: 1px solid #444;
            border-bottom: 1px solid #444;
            padding: 5px 8px; font-weight: bold;
        }

        QPushButton {
            background: #0e639c; color: #fff;
            border: none; border-radius: 5px;
            padding: 6px 16px; font-size: 10pt; font-weight: 600;
            min-height: 32px;
        }
        QPushButton:hover    { background: #1177bb; }
        QPushButton:pressed  { background: #0a4a75; }
        QPushButton:disabled { background: #3a3a3a; color: #666; }

        QComboBox {
            background: #121212; color: #f1f1f1;
            border: 1px solid #3f3f3f; border-radius: 5px;
            padding: 4px 8px; font-size: 10pt; min-height: 28px;
        }
        QComboBox:focus { border: 1px solid #569cd6; }
        QComboBox::drop-down { border: none; width: 20px; }
        QComboBox QAbstractItemView {
            background: #121212; color: #f1f1f1;
            border: 1px solid #3f3f3f;
            selection-background-color: #264f78;
        }

        QCheckBox { color: #ddd; font-size: 10pt; }
        QCheckBox::indicator { width: 16px; height: 16px; }

        QTabWidget::pane {
            border: 1px solid #3a3a3a; border-radius: 6px;
            background: #252526; padding: 4px;
        }
        QTabBar::tab {
            background: #121212; color: #999;
            padding: 8px 20px; margin-right: 2px;
            border: 1px solid #3a3a3a; border-bottom: none;
            border-top-left-radius: 5px; border-top-right-radius: 5px;
            font-size: 10pt;
        }
        QTabBar::tab:selected {
            background: #252526; color: #569cd6; border-color: #569cd6;
            font-weight: bold;
        }
        QTabBar::tab:hover:!selected { background: #333; }

        QProgressBar {
            border: 1px solid #3a3a3a; border-radius: 5px;
            text-align: center; color: #ddd;
            background: #121212; min-height: 16px;
        }
        QProgressBar::chunk { background: #0e639c; border-radius: 4px; }

        QScrollBar:vertical {
            background: #121212; width: 10px; border-radius: 5px;
        }
        QScrollBar::handle:vertical {
            background: #555; border-radius: 5px; min-height: 30px;
        }
        QScrollBar::handle:vertical:hover { background: #777; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }

        QStatusBar { background: #1a1a1a; color: #aaa; font-size: 9pt; }
        QMenuBar { background: #1a1a1a; color: #ddd; }
        QMenuBar::item:selected { background: #333; }
        QMenu { background: #252526; color: #f1f1f1; border: 1px solid #3a3a3a; }
        QMenu::item:selected { background: #264f78; color: #fff; }

        QSplitter::handle { background: #333; }
        QFrame { color: #3a3a3a; }
        """

    
