# ==================== main.py ====================
import sys
from PyQt6.QtWidgets import QApplication
from main_window import MainWindow
from themes import ThemeManager

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Uploder")
    app.setOrganizationName("Uploder")
    
    # Инициализация менеджера тем
    theme_manager = ThemeManager()
    
    # Применяем тему ко всему приложению
    theme_manager.apply_theme(app)
    
    # Устанавливаем стиль Fusion для лучшей поддержки тем
    app.setStyle("Fusion")
    
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())

if __name__ == "__main__":
    main()