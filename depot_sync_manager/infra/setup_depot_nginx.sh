#!/usr/bin/env bash
# ==================== setup_depot_nginx.sh ====================
# Разворачивает read-only nginx для раздачи депо напрямую с диска
# Nextcloud (замена прямого чтения через WebDAV для лаунчера — паблиш
# через TESL-Manager остаётся через WebDAV в Nextcloud без изменений,
# см. CLAUDE.md "Read-only nginx перед Cloudflare"). Запускать на самом
# сервере (root по SSH), не отсюда — у Claude-сессии нет exec-доступа
# к продакшену, этот файл только готовится и передаётся оператору.
#
# ВАЖНО перед запуском:
#   1. Проверь, что server encryption в Nextcloud ВЫКЛЮЧЕНО (скрипт сам
#      проверит и откажется работать, если включено — см. ниже, почему).
#   2. Получи Origin-сертификат Cloudflare (SSL/TLS -> Origin Server ->
#      Create Certificate в дэшборде Cloudflare, 15 лет, бесплатно) и
#      положи его ДО запуска в:
#        /etc/ssl/tesl-depot/origin.pem   (сертификат)
#        /etc/ssl/tesl-depot/origin.key   (приватный ключ)
#      (пути можно переопределить флагами --cert/--key)
#   3. В Cloudflare: A/AAAA-запись на этот сервер, статус "Proxied"
#      (оранжевое облако), SSL/TLS mode = "Full (strict)".
#   4. В Cloudflare: Cache Rule для этого хоста — иначе Cloudflare по
#      умолчанию НЕ кэширует ответы, у которых в запросе есть заголовок
#      Authorization (Basic Auth), а нам нужно кэшировать /chunks/ —
#      см. вывод скрипта в конце, там точная инструкция.
#
# Использование:
#   sudo ./setup_depot_nginx.sh --domain dl.example.com \
#       --nc-user SkyrimDownloader --depot-subpath "1TB/TESS/Instances"
#
# Идемпотентен — повторный запуск с теми же аргументами безопасен
# (перезаписывает конфиг nginx, htpasswd не трогает, если уже существует).

set -euo pipefail

DOMAIN=""
NC_USER=""
DEPOT_SUBPATH=""
NC_WEB_USER="www-data"
OCC_PATH=""
CERT_PATH="/etc/ssl/tesl-depot/origin.pem"
KEY_PATH="/etc/ssl/tesl-depot/origin.key"
HTPASSWD_PATH="/etc/nginx/tesl-depot.htpasswd"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain)        DOMAIN="$2"; shift 2 ;;
        --nc-user)        NC_USER="$2"; shift 2 ;;
        --depot-subpath)  DEPOT_SUBPATH="$2"; shift 2 ;;
        --nc-web-user)    NC_WEB_USER="$2"; shift 2 ;;
        --occ)            OCC_PATH="$2"; shift 2 ;;
        --cert)           CERT_PATH="$2"; shift 2 ;;
        --key)            KEY_PATH="$2"; shift 2 ;;
        --htpasswd)       HTPASSWD_PATH="$2"; shift 2 ;;
        *) echo "Неизвестный аргумент: $1" >&2; exit 1 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Нужен root (sudo)." >&2
    exit 1
fi
if [[ -z "$DOMAIN" || -z "$NC_USER" || -z "$DEPOT_SUBPATH" ]]; then
    echo "Обязательны: --domain --nc-user --depot-subpath" >&2
    exit 1
fi

echo "== 1/7: Ищем occ (консоль Nextcloud) =="
if [[ -z "$OCC_PATH" ]]; then
    for candidate in /var/www/nextcloud/occ /var/www/html/nextcloud/occ /var/www/html/occ; do
        if [[ -f "$candidate" ]]; then
            OCC_PATH="$candidate"
            break
        fi
    done
fi
if [[ -z "$OCC_PATH" || ! -f "$OCC_PATH" ]]; then
    echo "❌ occ не найден по стандартным путям. Передай явно: --occ /путь/до/occ" >&2
    echo "   (найти вручную: find / -maxdepth 6 -name occ -type f 2>/dev/null)" >&2
    exit 1
fi
echo "   occ: $OCC_PATH"

run_occ() {
    sudo -u "$NC_WEB_USER" php "$OCC_PATH" "$@"
}

echo "== 2/7: Проверяем encryption (КРИТИЧНО — если включено, файлы на диске зашифрованы, читать их в обход Nextcloud нельзя) =="
ENC_STATUS="$(run_occ encryption:status 2>&1 || true)"
if echo "$ENC_STATUS" | grep -qi "enabled: true\|Encryption is enabled"; then
    echo "❌ Server-side encryption ВКЛЮЧЕНО в Nextcloud." >&2
    echo "   Файлы на диске — зашифрованные блобы, нгinx отдавал бы мусор." >&2
    echo "   Этот подход (прямое чтение с диска) не годится — нужен план Б" >&2
    echo "   (например периодический rsync/hardlink через сам WebDAV, а не с диска)." >&2
    exit 1
fi
echo "   ✅ Encryption выключено (или occ не смог явно подтвердить, что включено — вывод: $ENC_STATUS)"

echo "== 3/7: Определяем datadirectory =="
DATADIR="$(run_occ config:system:get datadirectory)"
if [[ -z "$DATADIR" || ! -d "$DATADIR" ]]; then
    echo "❌ Не удалось получить datadirectory через occ (получили: '$DATADIR')" >&2
    exit 1
fi
echo "   datadirectory: $DATADIR"

ALIAS_PATH="$DATADIR/$NC_USER/files/$DEPOT_SUBPATH"
echo "== 4/7: Проверяем итоговый путь =="
echo "   $ALIAS_PATH"
if [[ ! -d "$ALIAS_PATH" ]]; then
    echo "❌ Директория не существует. Проверь --nc-user/--depot-subpath." >&2
    exit 1
fi
# Не просто существование директории — ищем хотя бы одну реальную
# сборку с папкой chunks/ внутри (тот же урок, что уже стоил часов
# отладки в z2r_autobench/Zenith: директория может существовать, но
# быть пустой заглушкой на неверном layout'е — см. их CLAUDE.md
# "prefer probing for the actual file/thing you need over a directory
# existence check").
if ! find "$ALIAS_PATH" -maxdepth 2 -type d -name chunks | grep -q .; then
    echo "❌ Внутри $ALIAS_PATH не нашлось ни одной папки chunks/ на глубине 2." >&2
    echo "   Похоже это не тот путь — сверь --depot-subpath с реальной структурой депо." >&2
    exit 1
fi
echo "   ✅ Нашли минимум одну сборку с chunks/"

echo "== 5/7: Устанавливаем nginx (если ещё нет) =="
if ! command -v nginx >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y nginx apache2-utils
else
    command -v htpasswd >/dev/null 2>&1 || apt-get install -y apache2-utils
fi

echo "== 6/7: Basic Auth =="
if [[ -f "$HTPASSWD_PATH" ]]; then
    echo "   $HTPASSWD_PATH уже существует, не трогаю (удали вручную, если нужно пересоздать)."
else
    echo "   Создаём $HTPASSWD_PATH — введи пароль для пользователя '$NC_USER' (тот же,"
    echo "   что уже зашит в лаунчер как DAV_USERNAME/DAV_PASSWORD, для совместимости):"
    htpasswd -c "$HTPASSWD_PATH" "$NC_USER"
fi

if [[ ! -f "$CERT_PATH" || ! -f "$KEY_PATH" ]]; then
    echo "⚠️  Origin-сертификат Cloudflare не найден ($CERT_PATH / $KEY_PATH)."
    echo "   nginx -t ниже, скорее всего, провалится — положи сертификат и перезапусти скрипт,"
    echo "   либо доразверни вручную (см. докстринг файла для инструкции)."
fi

echo "== 7/7: Конфиг nginx =="
TEMPLATE="$(dirname "$0")/nginx-depot-read.conf.template"
if [[ ! -f "$TEMPLATE" ]]; then
    echo "❌ Не найден $TEMPLATE рядом со скриптом." >&2
    exit 1
fi
CONF_PATH="/etc/nginx/sites-available/${DOMAIN}.conf"
sed \
    -e "s#__DOMAIN__#${DOMAIN}#g" \
    -e "s#__ALIAS_PATH__#${ALIAS_PATH}#g" \
    -e "s#__HTPASSWD_PATH__#${HTPASSWD_PATH}#g" \
    -e "s#__ORIGIN_CERT__#${CERT_PATH}#g" \
    -e "s#__ORIGIN_KEY__#${KEY_PATH}#g" \
    "$TEMPLATE" > "$CONF_PATH"
ln -sf "$CONF_PATH" "/etc/nginx/sites-enabled/${DOMAIN}.conf"

if nginx -t; then
    systemctl reload nginx
    echo "✅ nginx настроен и перезагружен."
else
    echo "❌ nginx -t провалился — см. вывод выше (частая причина — отсутствующий сертификат, см. шаг 6)." >&2
    exit 1
fi

cat <<EOF

Готово. Проверить (с ЭТОГО же сервера, минуя Cloudflare, только TLS-хендшейк
может ругаться на самоподписанность self-signed от curl — это нормально,
Cloudflare Origin-сертификату доверяет только Cloudflare):

  curl -k -u '${NC_USER}:<пароль>' https://127.0.0.1/TESVAE/depot.json --resolve ${DOMAIN}:443:127.0.0.1

После того как DNS/Cloudflare прокси настроены — снаружи:

  curl -u '${NC_USER}:<пароль>' https://${DOMAIN}/TESVAE/depot.json

Не забудь Cache Rule в Cloudflare (Caching -> Cache Rules -> Create rule):
  If: Hostname equals ${DOMAIN}
  Then: Cache eligibility = Eligible for cache
(без этого Cloudflare не кэширует ответы с заголовком Authorization —
Basic Auth у нас всегда его шлёт, а весь смысл этой раздачи — кэш на edge).
EOF
