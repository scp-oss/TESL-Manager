# ==================== file_selector.py ====================
import os
import json
from pathlib import Path
from PyQt6.QtWidgets import QFileDialog
from PyQt6.QtCore import QObject, pyqtSignal


class FileSelector(QObject):
    """Класс для управления конфигурацией."""
    folder_selected = pyqtSignal(str)
    config_updated  = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.config_file = Path(os.getenv("APPDATA") or Path.home()) / "Uploder" / "config.json"
        self.config = self._load_config()

    def _load_config(self) -> dict:
        try:
            if self.config_file.exists():
                with open(self.config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
            else:
                config = self._default_config()
        except Exception as e:
            print(f"Ошибка загрузки конфига: {e}")
            config = self._default_config()

        # Мигрируем старый формат: local_dir + excludes → components
        self._migrate_legacy(config)
        return config

    @staticmethod
    def _default_config() -> dict:
        from config import DEFAULT_COMPONENTS_CONFIG
        return {
            "components": {
                name: dict(cfg)
                for name, cfg in DEFAULT_COMPONENTS_CONFIG.items()
            },
            "webdav": {
                "server_url":  "",
                "username":    "",
                "password":    "",
                "remote_path": "/",
                "verify_ssl":  True,
            },
        }

    @staticmethod
    def _migrate_legacy(config: dict):
        """Переносим старые local_dir + excludes в компонентную структуру."""
        from config import DEFAULT_COMPONENTS_CONFIG, COMPONENT_NAMES
        if "components" not in config:
            config["components"] = {}
        for name in COMPONENT_NAMES:
            if name not in config["components"]:
                config["components"][name] = dict(DEFAULT_COMPONENTS_CONFIG[name])

    def save_config(self) -> bool:
        try:
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            self.config_updated.emit(self.config)
            return True
        except Exception as e:
            print(f"Ошибка сохранения конфига: {e}")
            return False

    def set_webdav_config(
        self,
        server_url: str,
        username:   str,
        password:   str,
        remote_path: str = "/",
        verify_ssl:  bool = True,
    ) -> bool:
        self.config["webdav"] = {
            "server_url":  server_url.rstrip("/"),
            "username":    username,
            "password":    password,
            "remote_path": remote_path.strip("/"),
            "verify_ssl":  verify_ssl,
        }
        return self.save_config()

    def get_webdav_config(self) -> dict:
        return self.config.get("webdav", {})

    def get_components(self) -> dict:
        return self.config.get("components", {})

    # Совместимость со старым кодом (manifest_manager и др.)
    def get_local_folder(self) -> str:
        """Возвращает первый заданный local_dir среди компонентов (для совместимости)."""
        for comp in self.config.get("components", {}).values():
            if comp.get("local_dir"):
                return comp["local_dir"]
        return ""

    def get_excludes(self) -> list:
        """Возвращает объединённые исключения всех компонентов (для совместимости)."""
        result = []
        for comp in self.config.get("components", {}).values():
            result.extend(comp.get("excludes", []))
        return result
