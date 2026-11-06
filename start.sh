#!/usr/bin/env bash
# Запуск «Дневника гипертоника»: бот и личный кабинет (SPEC.md, раздел 16.3).
#
#   ./start.sh     — собрать образ (если нужно) и запустить контейнеры
#   ./restart.sh   — перезапуск (подхватывает .env и код после git pull)
#   ./stop.sh      — остановка (данные в томах сохраняются)
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

docker compose up -d --build
echo "Запущено. Состояние: docker compose ps"
