#!/usr/bin/env bash
# Перезапуск «Дневника гипертоника»: пересобирает образ и пересоздаёт контейнеры.
#
# Пересоздание обязательно: переменные окружения из .env и подключение к сетям
# (см. docker-compose.override.yml) зашиваются в контейнер при создании,
# `docker restart` их не подхватывает. После git pull — именно эта команда.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    echo "ОШИБКА: docker не найден." >&2
    exit 1
fi
if [ ! -f .env ]; then
    echo "ОШИБКА: нет .env — создайте из .env.example и заполните секреты." >&2
    exit 1
fi

docker compose up -d --build --force-recreate
echo "Перезапущено. Состояние: docker compose ps"
