#!/usr/bin/env bash
# Остановка «Дневника гипертоника»: контейнеры убираются, данные сохраняются.
#
# База SQLite и бэкапы живут в томах (bot-data) и не затрагиваются.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
    echo "ОШИБКА: docker не найден." >&2
    exit 1
fi

docker compose down
echo "Остановлено. Данные в томах сохранены."
