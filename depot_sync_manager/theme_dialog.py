# ==================== theme_dialog.py ====================
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, 
    QLabel, QComboBox, QGroupBox, QRadioButton,
    QButtonGroup, QApplication, QWidget, QLineEdit, QCheckBox
)
from PyQt6.QtGui import QFont, QIcon
from PyQt6.QtCore import Qt, pyqtSignal
from themes import ThemeManager

class ThemeDialog(QDialog):
    """Диалог выбора темы"""
    theme_changed = pyqtSignal(str)
    
    def __init__(self, parent=None, current_theme="system"):
        super().__init__(parent)
        self.setWindowTitle("🎨 Настройки темы")
        self.resize(600, 400)
        self.current_theme = current_theme
        self.theme_manager = ThemeManager()
        self.init_ui()
        
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # Заголовок
        title_label = QLabel("🎨 Выберите тему оформления")
        title_label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet("padding: 15px;")
        layout.addWidget(title_label)
        
        # Описание
        desc_label = QLabel("Выберите тему, которая лучше всего подходит для ваших глаз и рабочей среды.")
        desc_label.setWordWrap(True)
        desc_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(desc_label)
        
        # Группа выбора темы
        theme_group = QGroupBox("Доступные темы")
        theme_layout = QVBoxLayout()
        
        # Кнопки для выбора темы
        self.theme_buttons = QButtonGroup(self)
        
        themes = [
            ("system", "🖥️  Системная тема", "Автоматически определяет тему вашей системы"),
            ("light", "☀️  Светлая тема", "Классическая светлая тема для дневного времени"),
            ("dark", "🌙  Темная тема", "Темная тема для ночной работы, бережет глаза")
        ]
        
        for theme_id, theme_name, theme_desc in themes:
            theme_widget = self.create_theme_widget(theme_id, theme_name, theme_desc)
            theme_layout.addWidget(theme_widget)
        
        theme_group.setLayout(theme_layout)
        layout.addWidget(theme_group)
        
        # Кнопки
        button_layout = QHBoxLayout()
        
        btn_apply = QPushButton("✅ Применить")
        btn_cancel = QPushButton("❌ Отмена")
        btn_preview = QPushButton("👁️  Предпросмотр")
        
        btn_apply.clicked.connect(self.apply_theme)
        btn_cancel.clicked.connect(self.reject)
        btn_preview.clicked.connect(self.preview_theme)
        
        button_layout.addWidget(btn_apply)
        button_layout.addWidget(btn_preview)
        button_layout.addWidget(btn_cancel)
        
        layout.addLayout(button_layout)
        
        # Устанавливаем текущую тему
        self.set_current_theme(self.current_theme)
        
    def create_theme_widget(self, theme_id, theme_name, theme_desc):
        """Создание виджета для темы"""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Радиокнопка
        radio = QRadioButton(theme_name)
        radio.theme_id = theme_id
        radio.setFont(QFont("Segoe UI", 10))
        
        # Описание
        desc_label = QLabel(theme_desc)
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("color: #666666; font-size: 9pt;")
        
        layout.addWidget(radio)
        layout.addWidget(desc_label, 1)
        
        self.theme_buttons.addButton(radio)
        
        return widget
    
    def set_current_theme(self, theme_id):
        """Установка текущей темы"""
        for button in self.theme_buttons.buttons():
            if hasattr(button, 'theme_id') and button.theme_id == theme_id:
                button.setChecked(True)
                break
    
    def get_selected_theme(self):
        """Получение выбранной темы"""
        for button in self.theme_buttons.buttons():
            if button.isChecked() and hasattr(button, 'theme_id'):
                return button.theme_id
        return "system"
    
    def preview_theme(self):
        """Предпросмотр темы"""
        theme_id = self.get_selected_theme()
        
        # Применяем тему к самому диалогу для предпросмотра
        stylesheet = self.theme_manager.get_stylesheet(
            self.theme_manager.detect_system_theme() if theme_id == "system" else theme_id
        )
        self.setStyleSheet(stylesheet)
        
    def apply_theme(self):
        """Применение выбранной темы"""
        theme_id = self.get_selected_theme()
        self.theme_changed.emit(theme_id)
        self.accept()