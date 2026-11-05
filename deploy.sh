#!/usr/bin/env bash
# Развёртывание «Дневника гипертоника» в Docker (SPEC.md, раздел 16.3).
#
#   ./deploy.sh          — собрать образ и запустить бота и личный кабинет
#   ./deploy.sh https    — то же + HTTPS (Caddy с Let's Encrypt)
#   ./deploy.sh logs     — логи всех сервисов
#   ./deploy.sh ps       — состояние контейнеров
#   ./deploy.sh down     — остановить и убрать контейнеры
#
# Секреты (BOT_TOKEN, WEB_SECRET_KEY) задаются только в .env — файл не попадает в git.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    echo "ОШИБКА: docker не найден. Установите Docker и повторите запуск." >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "ОШИБКА: плагин docker compose не найден (нужен Compose V2)." >&2
    exit 1
fi

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Файл .env не найден — создан из .env.example."
    echo "Заполните BOT_TOKEN и WEB_SECRET_KEY (и CABINET_URL, если развёрнут кабинет),"
    echo "затем запустите ./deploy.sh снова."
    exit 1
fi

# Проверка секретов: только факт заполненности, значения не выводятся.
if ! grep -Eq '^BOT_TOKEN=.+' .env; then
    echo "ОШИБКА: в .env не задан BOT_TOKEN." >&2
    exit 1
fi
if ! grep -Eq '^WEB_SECRET_KEY=.+' .env; then
    echo "ОШИБКА: в .env не задан WEB_SECRET_KEY (без него личный кабинет не работает)." >&2
    exit 1
fi

case "${1:-up}" in
    up)
        docker compose up -d --build
        ;;
    https)
        if ! grep -Eq '^CABINET_URL=https?://.+' .env; then
            echo "ОШИБКА: для HTTPS в .env нужен CABINET_URL (например https://cabinet.example.org)." >&2
            exit 1
        fi
        docker compose --profile https up -d --build
        ;;
    logs)
        docker compose logs -f --tail=100
        ;;
    ps)
        docker compose ps
        ;;
    down)
        docker compose --profile https down
        ;;
    *)
        echo "Неизвестная команда: $1 (допустимы: up, https, logs, ps, down)" >&2
        exit 1
        ;;
esac

echo
docker compose ps
