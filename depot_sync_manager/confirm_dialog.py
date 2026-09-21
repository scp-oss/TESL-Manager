# ==================== confirm_dialog.py ====================
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, 
    QLabel, QListWidget, QTabWidget, QWidget
)
from PyQt6.QtCore import Qt

class ConfirmSyncDialog(QDialog):
    """Диалог подтверждения синхронизации"""
    def __init__(self, parent=None, operations=None):
        super().__init__(parent)
        self.operations = operations or {"upload": [], "delete": [], "keep": []}
        self.setWindowTitle("Подтверждение синхронизации")
        self.resize(800, 600)
        self.init_ui()
    
    def init_ui(self):
        layout = QVBoxLayout(self)
        
        # Заголовок
        title_label = QLabel("🔄 Подтверждение зеркальной синхронизации")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)
        
        # Информация о плане
        info_label = QLabel(
            "<b>📋 План синхронизации:</b><br><br>"
            f"📤 Загрузить на сервер: {len(self.operations['upload'])} файлов<br>"
            f"🗑️  Удалить с сервера: {len(self.operations['delete'])} файлов<br>"
            f"✅ Оставить без изменений: {len(self.operations['keep'])} файлов<br><br>"
            "<i>После синхронизации сервер будет точной копией локальной папки (с учетом исключений).</i>"
        )
        layout.addWidget(info_label)
        
        # Вкладки для деталей
        tabs = QTabWidget()
        
        # Вкладка файлов для загрузки
        if self.operations["upload"]:
            upload_tab = QWidget()
            upload_layout = QVBoxLayout(upload_tab)
            
            upload_label = QLabel(f"<b>📤 Файлы для загрузки ({len(self.operations['upload'])}):</b>")
            upload_layout.addWidget(upload_label)
            
            upload_list = QListWidget()
            for op in self.operations["upload"][:100]:
                size_mb = op.get('size', 0) / 1024 / 1024
                upload_list.addItem(f"{op['path']} ({size_mb:.1f} MB) - {op.get('reason', '')}")
            
            if len(self.operations["upload"]) > 100:
                upload_list.addItem(f"... и еще {len(self.operations['upload']) - 100} файлов")
            
            upload_layout.addWidget(upload_list)
            tabs.addTab(upload_tab, f"📤 Загрузить ({len(self.operations['upload'])})")
        
        # Вкладка файлов для удаления
        if self.operations["delete"]:
            delete_tab = QWidget()
            delete_layout = QVBoxLayout(delete_tab)
            
            warning_label = QLabel("⚠️ <b>Внимание! Эти файлы будут удалены с сервера:</b>")
            delete_layout.addWidget(warning_label)
            
            delete_list = QListWidget()
            for op in self.operations["delete"][:100]:
                size_mb = op.get('size', 0) / 1024 / 1024
                delete_list.addItem(f"{op['path']} ({size_mb:.1f} MB) - {op.get('reason', '')}")
            
            if len(self.operations["delete"]) > 100:
                delete_list.addItem(f"... и еще {len(self.operations['delete']) - 100} файлов")
            
            delete_layout.addWidget(delete_list)
            tabs.addTab(delete_tab, f"🗑️ Удалить ({len(self.operations['delete'])})")
        
        if tabs.count() > 0:
            layout.addWidget(tabs)
        
        # Кнопки
        button_layout = QHBoxLayout()
        
        btn_confirm = QPushButton("✅ Начать синхронизацию")
        btn_cancel = QPushButton("❌ Отмена")
        
        btn_confirm.clicked.connect(self.accept)
        btn_cancel.clicked.connect(self.reject)
        
        button_layout.addWidget(btn_confirm)
        button_layout.addWidget(btn_cancel)
        button_layout.addStretch()
        
        layout.addLayout(button_layout)