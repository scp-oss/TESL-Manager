# ==================== manifest_manager.py ====================
import os
import json
import hashlib
import fnmatch
from pathlib import Path
from datetime import datetime
from PyQt6.QtCore import QObject, pyqtSignal

class ManifestManager(QObject):
    """Менеджер манифеста файлов с улучшенной обработкой исключений"""
    manifest_created = pyqtSignal(int, int)  # file_count, total_size
    progress_updated = pyqtSignal(int, int, str)  # current, total, filename
    scan_started = pyqtSignal(int)  # total_files_found
    scan_finished = pyqtSignal(int, int)  # included_files, excluded_files
    
    def __init__(self, config):
        super().__init__()
        self.config = config
        from config import APPDATA_DIR
        self.manifest_file = APPDATA_DIR / "manifest.json"
        
    def should_exclude(self, path, excludes):
        """Проверка, нужно ли исключить файл/папку"""
        if not excludes:
            return False
            
        path_str = str(path)
        path_name = path.name
        
        for pattern in excludes:
            # Очищаем паттерн
            pattern = pattern.strip()
            if not pattern:
                continue
            
            # Паттерн для директории (заканчивается на / или \)
            if pattern.endswith('/') or pattern.endswith('\\'):
                pattern_dir = pattern.rstrip('/\\').lower()
                path_parts = [p.lower() for p in path_str.split(os.sep)]
                
                # Проверяем, содержит ли путь эту директорию
                if pattern_dir in path_parts:
                    return True
            
            # Паттерн для файлов
            else:
                # Проверяем по имени файла
                try:
                    if fnmatch.fnmatch(path_name.lower(), pattern.lower()):
                        return True
                except:
                    # Если fnmatch не сработал, делаем простую проверку
                    if pattern.lower() == path_name.lower():
                        return True
                
                # Проверяем по полному пути (относительно корневой папки)
                try:
                    if fnmatch.fnmatch(path_str.lower(), pattern.lower()):
                        return True
                except:
                    pass
        
        return False
    
    def calculate_file_hash(self, file_path):
        """Вычисление хэша файла с обработкой ошибок"""
        try:
            if not os.path.exists(file_path):
                return ""
            
            file_size = os.path.getsize(file_path)
            if file_size == 0:
                # Для пустых файлов возвращаем специальный хэш
                return hashlib.sha256(b"").hexdigest()
            
            h = hashlib.sha256()
            buffer_size = 65536  # 64KB
            
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(buffer_size), b""):
                    h.update(chunk)
            
            return h.hexdigest()
            
        except PermissionError:
            print(f"❌ Нет доступа к файлу: {file_path}")
            return ""
        except Exception as e:
            print(f"⚠️ Ошибка вычисления хэша {file_path}: {e}")
            return ""
    
    def scan_directory(self, local_dir, excludes):
        """Сканирование директории и возврат всех файлов"""
        all_files = []
        excluded_files = []
        excluded_dirs = []
        
        try:
            for root, dirs, files in os.walk(local_dir):
                # Создаем копию директорий для модификации
                dirs_to_remove = []
                
                # Проверяем директории на исключение
                for dir_name in dirs:
                    dir_path = Path(root) / dir_name
                    if self.should_exclude(dir_path, excludes):
                        dirs_to_remove.append(dir_name)
                        excluded_dirs.append(str(dir_path.relative_to(local_dir)))
                        print(f"  🚫 Исключена директория: {dir_path.relative_to(local_dir)}")
                
                # Удаляем исключенные директории из списка для обхода
                for dir_name in dirs_to_remove:
                    dirs.remove(dir_name)
                
                # Проверяем файлы на исключение
                for file_name in files:
                    file_path = Path(root) / file_name
                    if self.should_exclude(file_path, excludes):
                        excluded_files.append(str(file_path.relative_to(local_dir)))
                        print(f"  🚫 Исключен файл: {file_path.relative_to(local_dir)}")
                    else:
                        all_files.append(file_path)
            
            return all_files, excluded_files, excluded_dirs
            
        except Exception as e:
            print(f"❌ Ошибка сканирования директории {local_dir}: {e}")
            return [], [], []
    
    def create_manifest(self):
        """Создание манифеста файлов с учетом исключений и диагностикой"""
        local_dir = self.config.get("local_dir", "")
        excludes = self.config.get("excludes", [])
        
        if not local_dir or not os.path.exists(local_dir):
            return False, "Локальная папка не выбрана или не существует"
        
        print(f"🔍 Начинаем создание манифеста...")
        print(f"📂 Папка: {local_dir}")
        print(f"🚫 Исключения: {excludes}")
        
        try:
            # Проверяем существующий манифест для определения версии
            version_number = 1
            if self.manifest_file.exists():
                try:
                    with open(self.manifest_file, 'r', encoding='utf-8') as f:
                        existing_manifest = json.load(f)
                        version_number = existing_manifest.get("version_number", 1) + 1
                except Exception as e:
                    print(f"⚠️ Ошибка чтения существующего манифеста: {e}")
                    version_number = 1
            
            # Сканируем директорию
            print(f"📁 Сканируем файлы (с учетом исключений)...")
            all_files, excluded_files, excluded_dirs = self.scan_directory(local_dir, excludes)
            
            if not all_files:
                return False, f"Не найдено файлов для добавления в манифест. Исключено: {len(excluded_files)} файлов, {len(excluded_dirs)} директорий"
            
            print(f"📊 Найдено файлов: {len(all_files)}")
            print(f"🚫 Исключено файлов: {len(excluded_files)}")
            print(f"🚫 Исключено директорий: {len(excluded_dirs)}")
            
            manifest = {
                "version": f"1.0.{version_number}",
                "version_number": version_number,
                "created_at": datetime.now().isoformat(),
                "local_dir": local_dir,
                "excludes": excludes,
                "excluded_files": excluded_files[:100],  # Сохраняем до 100 исключенных файлов для отладки
                "excluded_dirs": excluded_dirs[:50],     # Сохраняем до 50 исключенных директорий
                "scan_info": {
                    "total_scanned": len(all_files) + len(excluded_files),
                    "included": len(all_files),
                    "excluded": len(excluded_files),
                    "excluded_dirs_count": len(excluded_dirs)
                },
                "files": {}
            }
            
            file_count = 0
            total_size = 0
            processed_files = 0
            
            total_files = len(all_files)
            
            # Отправляем сигнал о начале сканирования
            self.scan_started.emit(total_files)
            
            # Обрабатываем файлы
            for file_path in all_files:
                processed_files += 1
                
                # Отправляем прогресс
                rel_path = str(file_path.relative_to(local_dir)).replace("\\", "/")
                self.progress_updated.emit(processed_files, total_files, rel_path)
                
                try:
                    # Получаем информацию о файле
                    stat = file_path.stat()
                    file_size = stat.st_size
                    
                    # Пропускаем слишком большие файлы (более 2GB)
                    if file_size > 2 * 1024 * 1024 * 1024:
                        print(f"⚠️ Пропускаем слишком большой файл: {rel_path} ({file_size/1024/1024:.1f} MB)")
                        continue
                    
                    # Вычисляем хэш (только для файлов до 100MB для скорости)
                    file_hash = ""
                    if file_size < 100 * 1024 * 1024:
                        file_hash = self.calculate_file_hash(file_path)
                    
                    if file_hash or file_size == 0:  # Пустые файлы тоже добавляем
                        manifest["files"][rel_path] = {
                            "size": file_size,
                            "modified": stat.st_mtime,
                            "hash": f"sha256:{file_hash}" if file_hash else "empty"
                        }
                        
                        file_count += 1
                        total_size += file_size
                        
                        if processed_files % 100 == 0:
                            print(f"  Обработано: {processed_files}/{total_files} файлов")
                            
                except PermissionError as e:
                    print(f"❌ Нет доступа к файлу {file_path}: {e}")
                    continue
                except Exception as e:
                    print(f"⚠️ Ошибка обработки файла {file_path}: {e}")
                    continue
            
            # Проверяем, есть ли файлы в манифесте
            if not manifest["files"]:
                return False, "Не удалось добавить ни одного файла в манифест. Возможные причины:\n" \
                             "1. Все файлы исключены\n" \
                             "2. Нет доступа к файлам\n" \
                             "3. Файлы слишком большие"
            
            # Сохраняем манифест
            self.manifest_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.manifest_file, 'w', encoding='utf-8') as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            
            # Отправляем сигнал о завершении сканирования
            self.scan_finished.emit(file_count, len(excluded_files))
            
            # Отправляем сигнал о создании манифеста
            self.manifest_created.emit(file_count, total_size)
            
            # Логируем результат
            summary = (
                f"✅ Создан манифест v{version_number}\n"
                f"📊 Файлов в манифесте: {file_count}\n"
                f"💾 Общий размер: {total_size/1024/1024:.2f} MB\n"
                f"🚫 Исключено файлов: {len(excluded_files)}\n"
                f"🚫 Исключено директорий: {len(excluded_dirs)}"
            )
            
            print(summary)
            return True, summary
            
        except Exception as e:
            error_msg = f"Ошибка создания манифеста: {e}"
            print(f"❌ {error_msg}")
            return False, error_msg
    
    def load_manifest(self):
        """Загрузка существующего манифеста (поддерживает оба формата)"""
        try:
            if not self.manifest_file.exists():
                return None
            
            with open(self.manifest_file, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            
            if "files" not in manifest:
                manifest["files"] = {}
            
            # Нормализуем поля которых может не быть в новом формате
            if "version" not in manifest:
                # Новый формат использует build_id вместо version
                manifest["version"] = manifest.get("build_id", "depot")
            if "version_number" not in manifest:
                manifest["version_number"] = manifest.get("build_number", 1)
            if "scan_info" not in manifest:
                manifest["scan_info"] = {
                    "total_scanned": len(manifest.get("files", {})),
                    "included": len(manifest.get("files", {})),
                    "excluded": 0,
                    "excluded_dirs_count": 0
                }
            
            return manifest
            
        except json.JSONDecodeError as e:
            print(f"❌ Ошибка парсинга манифеста: {e}")
            return None
        except Exception as e:
            print(f"❌ Ошибка загрузки манифеста: {e}")
            return None
    
    def get_manifest_info(self):
        """Получение информации о манифесте"""
        manifest = self.load_manifest()
        if not manifest:
            return None
        
        files = manifest.get("files", {})
        
        # Считаем размер совместимо с обоими форматами
        total_size = 0
        for v in files.values():
            if isinstance(v, dict):
                total_size += v.get("size", 0)
            # Новый формат (строка "sha256:hex") — размера нет
        
        return {
            "version": manifest.get("version", "depot"),
            "version_number": manifest.get("version_number", 1),
            "file_count": len(files),
            "total_size": total_size,
            "created_at": manifest.get("created_at", ""),
            "excludes": manifest.get("excludes", []),
            "scan_info": manifest.get("scan_info", {}),
            "local_dir": manifest.get("local_dir", "")
        }
    
    def verify_local_files(self):
        """Верификация локальных файлов по манифесту"""
        local_dir = self.config.get("local_dir", "")
        manifest = self.load_manifest()
        
        if not manifest or not local_dir:
            return False, "Манифест не создан или папка не выбрана"
        
        print(f"🔍 Начинаем верификацию файлов...")
        print(f"📂 Папка: {local_dir}")
        print(f"📄 Файлов в манифесте: {len(manifest.get('files', {}))}")
        
        try:
            missing_files = []
            corrupted_files = []
            valid_files = 0
            extra_files = []
            
            files = manifest.get("files", {})
            total_files = len(files)
            checked_files = 0
            
            # 1. Проверяем файлы из манифеста
            for rel_path, file_info in files.items():
                checked_files += 1
                self.progress_updated.emit(checked_files, total_files, f"Проверка: {rel_path}")
                
                local_path = Path(local_dir) / rel_path

                # ── Два формата манифеста ────────────────────────────────────
                # Новый (release_manager): file_info = "sha256:hexhash"
                # Старый (manifest_manager): file_info = {"size": N, "hash": "sha256:..."}
                if isinstance(file_info, str):
                    # Новый формат
                    raw_hash = file_info.replace("sha256:", "").strip()
                    expected_hash = raw_hash
                    expected_size = None   # размер в новом формате не хранится
                elif isinstance(file_info, dict):
                    # Старый формат
                    expected_hash = file_info.get("hash", "").replace("sha256:", "").strip()
                    expected_size = file_info.get("size", None)
                else:
                    expected_hash = ""
                    expected_size = None
                # ─────────────────────────────────────────────────────────────

                if not local_path.exists():
                    missing_files.append(rel_path)
                    print(f"  ❌ Отсутствует: {rel_path}")
                    continue
                
                # Проверяем размер (только если есть в манифесте)
                if expected_size is not None:
                    try:
                        actual_size = os.path.getsize(local_path)
                        if actual_size != expected_size:
                            corrupted_files.append(
                                f"{rel_path} (размер: {actual_size} != {expected_size})"
                            )
                            print(f"  ⚠️  Размер не совпадает: {rel_path}")
                            continue
                    except Exception:
                        corrupted_files.append(f"{rel_path} (ошибка проверки размера)")
                        continue
                
                # Проверяем хэш
                if expected_hash and expected_hash != "empty":
                    actual_hash = self.calculate_file_hash(local_path)
                    if actual_hash != expected_hash:
                        corrupted_files.append(f"{rel_path} (хэш не совпадает)")
                        print(f"  ⚠️  Хэш не совпадает: {rel_path}")
                        continue
                
                valid_files += 1
            
            # 2. Ищем лишние файлы (которые есть локально, но нет в манифесте)
            print(f"🔍 Поиск лишних файлов...")
            
            # Получаем исключения из манифеста
            excludes = manifest.get("excludes", [])
            
            # Сканируем локальные файлы (с теми же исключениями)
            all_local_files, excluded_files, excluded_dirs = self.scan_directory(local_dir, excludes)
            
            # Преобразуем пути для сравнения
            manifest_paths = set(files.keys())
            local_paths = {str(f.relative_to(local_dir)).replace("\\", "/") for f in all_local_files}
            
            # Находим файлы которые есть локально, но нет в манифесте
            extra_files = list(local_paths - manifest_paths)
            
            result = {
                "total_files": total_files,
                "valid_files": valid_files,
                "missing_files": missing_files,
                "corrupted_files": corrupted_files,
                "extra_files": extra_files[:50],  # Ограничиваем список
                "extra_files_count": len(extra_files),
                "excluded_files": excluded_files,
                "excluded_dirs": excluded_dirs,
                "status": "complete"
            }
            
            # Определяем статус
            if missing_files or corrupted_files:
                result["status"] = "issues"
            elif extra_files:
                result["status"] = "extra_files"
            else:
                result["status"] = "perfect"
            
            # Логируем результат
            print(f"📊 Результат верификации:")
            print(f"  ✅ Валидных: {valid_files}/{total_files}")
            print(f"  ❌ Отсутствует: {len(missing_files)}")
            print(f"  ⚠️  Повреждено: {len(corrupted_files)}")
            print(f"  📝 Лишних файлов: {len(extra_files)}")
            print(f"  🚫 Исключено при сканировании: {len(excluded_files)} файлов")
            
            return True, result
            
        except Exception as e:
            error_msg = f"Ошибка верификации: {e}"
            print(f"❌ {error_msg}")
            return False, error_msg
    
    def update_excludes_in_manifest(self, new_excludes):
        """Обновление списка исключений в существующем манифесте"""
        try:
            manifest = self.load_manifest()
            if not manifest:
                return False, "Манифест не найден"
            
            # Обновляем исключения
            manifest["excludes"] = new_excludes
            
            # Сохраняем обновленный манифест
            with open(self.manifest_file, 'w', encoding='utf-8') as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
            
            print(f"✅ Обновлены исключения в манифесте: {len(new_excludes)} правил")
            return True, "Исключения обновлены в манифесте"
            
        except Exception as e:
            error_msg = f"Ошибка обновления исключений: {e}"
            print(f"❌ {error_msg}")
            return False, error_msg