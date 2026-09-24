# ==================== panel_client.py ====================
"""
PanelHTTP — транспорт для публикации депо напрямую в TESL-Panel (см.
scp-oss/TESL-Panel), минуя Nextcloud/WebDAV. Реализует ТОТ ЖЕ публичный
интерфейс, что и NextcloudDAV в depot_sync_manager.py (exists/mkcol/put/
put_file/get_bytes/list_chunk_ids/test_connection/close) — DepotSyncManager
конструирует один или другой в зависимости от config["backend"]
("webdav" | "panel"), сам код execute_sync()/ensure_depot_structure() не
меняется ни на строчку, см. CLAUDE.md "Публикация напрямую в наш сервис".

mkcol() здесь — no-op (всегда True): панель создаёт родительские папки
сама при первом PUT в них, отдельного MKCOL-запроса не нужно — но метод
оставлен с той же сигнатурой, чтобы вызывающий код (`ensure_depot_structure`,
`_ensure_chunk_subdir`) не знал и не заботился, какой транспорт активен.
"""
import base64
import json
import time
from typing import List, Optional, Set, Tuple

import requests


TIMEOUT_CONNECT = 15
# 300с, не 120 — согласовано с TESL-Panel's nginx (client_body_timeout/
# proxy_read_timeout/proxy_send_timeout, см. её nginx-tesl-panel.conf.
# template) после живого инцидента 2026-09-24: клиентский таймаут
# должен быть не короче серверного, иначе на медленном канале клиент
# первым обрывает соединение при загрузке крупного pack-файла, и
# сообщение об ошибке выглядит как обрыв сети, а не как настоящий 413/
# таймаут сервера.
TIMEOUT_PUT     = 300
TIMEOUT_GET     = 60

MAX_RETRIES   = 3
RETRY_BACKOFF = (1, 3, 7)


# ── "Код настройки" — один base64(JSON) с адресом панели и токеном ──────────
# (см. TESL-Panel::panel/app.py::_generate_setup_code / /admin/settings) —
# прямой запрос пользователя: панель сама генерирует один код, вставляется
# один раз в GUI (main_window.py's Settings page), вместо URL+токена по
# отдельности. Формат зеркалит серверную сторону 1:1 — если он там
# изменится, менять здесь тоже, они не идут через общий модуль (разные
# репозитории/языковые рантаймы для этого куска нет смысла разделять
# дальше, чем уже есть).

def decode_setup_code(code: str) -> Optional[dict]:
    """{"base_url": ..., "token": ...} или None, если код нечитаем."""
    try:
        raw = base64.b64decode(code.strip(), validate=False)
        payload = json.loads(raw.decode("utf-8"))
        base_url = str(payload.get("base_url", "")).strip().rstrip("/")
        token    = str(payload.get("token", "")).strip()
        if not base_url or not token:
            return None
        return {"base_url": base_url, "token": token}
    except Exception:
        return None


def encode_setup_code(base_url: str, token: str) -> str:
    """Обратное к decode_setup_code() — используется, чтобы при повторном
    открытии окна показать в поле кода то же значение, что уже сохранено
    (base_url/token читаются из cfg["panel"], а не только из свежевведённого
    кода), а не оставлять поле пустым для уже подключённых пользователей."""
    payload = {"v": 1, "base_url": base_url.rstrip("/"), "token": token}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


class PanelHTTP:
    """build_id — стабильный UUID из TESL-Panel's builds_db.py (2026-09-23:
    прямой запрос пользователя, имя сборки не должно быть ключом связи
    между панелью и менеджером, поскольку сборки создаются независимо в
    обоих местах — нужен настоящий id). Имя остаётся только для отображения
    (например, DepotManifest.app_id) — все реальные /api/depot/... пути
    идут по build_id."""

    def __init__(
        self,
        base_url:   str,
        build_id:   str,
        token:      str,
        verify_ssl: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.build_id = build_id
        self.session  = requests.Session()
        self.session.verify = verify_ssl
        self.session.headers.update({
            "User-Agent":    "Uploder-Panel/1.0",
            "Authorization": f"Bearer {token}",
        })
        self.last_response = None
        # Живой случай (2026-09-24): put_file() глотал реальную причину
        # отказа (`except Exception: return False`) — реальный крэш
        # публикации на проде оказался 413 от nginx (client_max_body_size
        # был 64MB, рассчитан на одиночные чанки по 4MB, никто не поднял
        # его, когда появились pack-файлы по 256MB+), но диалог в GUI
        # показывал только голое "Не удалось загрузить pack-00001.bin" —
        # без этого текста причину нашли бы не сразу. last_error — то же,
        # что last_response, но переживает и сетевые исключения (когда
        # last_response остался бы от ПРЕДЫДУЩЕГО успешного запроса, вводя
        # в заблуждение), см. CLAUDE.md "Живой инцидент: 413 от nginx...".
        self.last_error = ""

    def _url(self, rel_path: str) -> str:
        return f"{self.base_url}/api/depot/{self.build_id}/{rel_path.strip('/')}"

    def _retry(self, fn, *args, **kwargs):
        last_exc = None
        for attempt, delay in enumerate(RETRY_BACKOFF[:MAX_RETRIES]):
            try:
                return fn(*args, **kwargs)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last_exc = e
                if attempt < MAX_RETRIES - 1:
                    time.sleep(delay)
        raise last_exc

    # ── Интерфейс, совместимый с NextcloudDAV ────────────────────────────────

    def exists(self, path: str) -> bool:
        try:
            r = self._retry(self.session.head, self._url(path), timeout=TIMEOUT_CONNECT)
            self.last_response = r
            return r.status_code == 200
        except Exception:
            return False

    def mkcol(self, path: str) -> bool:
        # См. докстринг модуля — панель создаёт директории сама при PUT.
        return True

    def put(self, path: str, data: bytes, content_type: str = "application/octet-stream") -> bool:
        self.last_error = ""
        try:
            r = self._retry(
                self.session.put, self._url(path), data=data,
                headers={"Content-Type": content_type}, timeout=TIMEOUT_PUT,
            )
            self.last_response = r
            if r.status_code in (200, 201, 204):
                return True
            self.last_error = f"HTTP {r.status_code}: {r.text[:300]}"
            return False
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return False

    def put_file(self, path: str, local_path, content_type: str = "application/octet-stream") -> bool:
        self.last_error = ""
        try:
            with open(local_path, "rb") as f:
                r = self._retry(
                    self.session.put, self._url(path), data=f,
                    headers={"Content-Type": content_type}, timeout=TIMEOUT_PUT,
                )
            self.last_response = r
            if r.status_code in (200, 201, 204):
                return True
            self.last_error = f"HTTP {r.status_code}: {r.text[:300]}"
            return False
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            return False

    def get_bytes(self, path: str) -> Optional[bytes]:
        try:
            r = self._retry(self.session.get, self._url(path), timeout=TIMEOUT_GET)
            self.last_response = r
            return r.content if r.status_code == 200 else None
        except Exception:
            return None

    def list_chunk_ids(self, chunks_path: str = "") -> Set[str]:
        # chunks_path игнорируется — на панели одна сборка = одна папка
        # chunks/, отдельный listing-эндпоинт уже знает, где искать (см.
        # TESL-Panel::panel/app.py depot_list_chunks).
        try:
            r = self._retry(
                self.session.get,
                f"{self.base_url}/api/depot/{self.build_id}/chunks",
                timeout=TIMEOUT_GET,
            )
            self.last_response = r
            if r.status_code != 200:
                return set()
            return set(r.json().get("chunk_ids", []))
        except Exception:
            return set()

    def test_connection(self) -> Tuple[bool, str]:
        try:
            r = self.session.get(
                f"{self.base_url}/api/depot/{self.build_id}/test",
                timeout=TIMEOUT_CONNECT,
            )
            self.last_response = r
            if r.status_code == 200:
                return True, "Подключено (TESL-Panel)"
            if r.status_code == 401:
                return False, "Неверный upload-токен"
            if r.status_code == 404:
                return False, f"Неизвестная сборка на панели: {self.build_id}"
            if r.status_code == 503:
                return False, "Панель не сконфигурирована для приёма публикаций (нет upload-токена на сервере)"
            return False, f"HTTP {r.status_code}"
        except requests.exceptions.SSLError as e:
            return False, f"SSL ошибка: {e}"
        except requests.exceptions.ConnectionError as e:
            return False, f"Нет соединения: {e}"
        except requests.exceptions.Timeout:
            return False, f"Таймаут подключения ({TIMEOUT_CONNECT}s)"
        except Exception as e:
            return False, str(e)

    # ── Сборки / файлы (для GUI: серверный список/создание/удаление/
    #    переименование сборок и вкладка "Файлы на сервере" — см.
    #    TESL-Panel::panel/app.py /api/builds и /api/depot/<build_id>/files,
    #    build_id — реальный ключ везде, имя только для отображения) ────────

    def list_builds(self) -> List[dict]:
        """[{"id": ..., "name": ..., "created_at": ...}, ...]"""
        try:
            r = self.session.get(f"{self.base_url}/api/builds", timeout=TIMEOUT_CONNECT)
            self.last_response = r
            if r.status_code != 200:
                return []
            return list(r.json().get("builds", []))
        except Exception:
            return []

    def create_build(self, name: str) -> Tuple[Optional[dict], str]:
        """(build_dict, "") при успехе, (None, причина) при отказе."""
        try:
            r = self.session.post(
                f"{self.base_url}/api/builds", json={"name": name}, timeout=TIMEOUT_CONNECT,
            )
            self.last_response = r
            if r.status_code == 201:
                return r.json(), ""
            if r.status_code == 401:
                return None, "Неверный upload-токен"
            try:
                return None, r.json().get("description", f"HTTP {r.status_code}")
            except Exception:
                return None, f"HTTP {r.status_code}"
        except Exception as e:
            return None, str(e)

    def delete_build(self, build_id: str) -> bool:
        try:
            r = self.session.delete(
                f"{self.base_url}/api/builds/{build_id}", timeout=TIMEOUT_CONNECT,
            )
            self.last_response = r
            return r.status_code == 200
        except Exception:
            return False

    def rename_build(self, build_id: str, new_name: str) -> Tuple[bool, str]:
        try:
            r = self.session.patch(
                f"{self.base_url}/api/builds/{build_id}",
                json={"name": new_name}, timeout=TIMEOUT_CONNECT,
            )
            self.last_response = r
            if r.status_code == 200:
                return True, ""
            try:
                return False, r.json().get("description", f"HTTP {r.status_code}")
            except Exception:
                return False, f"HTTP {r.status_code}"
        except Exception as e:
            return False, str(e)

    def list_files(self, build_id: Optional[str] = None) -> Optional[List[dict]]:
        """[{"path": ..., "size": ...}, ...] для сборки — если build_id не
        задан, используется self.build_id (см. __init__)."""
        bid = build_id or self.build_id
        try:
            r = self.session.get(
                f"{self.base_url}/api/depot/{bid}/files", timeout=TIMEOUT_GET,
            )
            self.last_response = r
            if r.status_code != 200:
                return None
            return list(r.json().get("files", []))
        except Exception:
            return None

    def delete_object(self, path: str) -> bool:
        try:
            r = self._retry(self.session.delete, self._url(path), timeout=TIMEOUT_CONNECT)
            self.last_response = r
            return r.status_code == 200
        except Exception:
            return False

    def close(self):
        self.session.close()
