# Образ бота и личного кабинета (SPEC.md, раздел 16.3).
# Один образ, две команды: `python -m bot` и `uvicorn web.cabinet.app:app`.
# Секреты в образ не зашиваются — только переменные окружения при запуске.
FROM python:3.12-slim AS builder

WORKDIR /app
RUN pip install --no-cache-dir uv
# Медленные сети: дефолтные 30 с роняют загрузку больших колёс (pillow).
ENV UV_HTTP_TIMEOUT=120

# Сначала зависимости (слой кэшируется, пока не меняется pyproject/uv.lock).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Затем код — и установка самого проекта в venv.
COPY bot ./bot
COPY web ./web
RUN uv sync --frozen --no-dev


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    DB_PATH=/app/data/bot.sqlite3

WORKDIR /app

# Непривилегированный пользователь и каталог данных (том SQLite).
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app/data

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/bot ./bot
COPY --from=builder --chown=appuser:appuser /app/web ./web
# pyproject.toml — единственный источник версии для «Здоровья системы» (§18.4).
COPY --from=builder --chown=appuser:appuser /app/pyproject.toml ./pyproject.toml

USER appuser
VOLUME /app/data

# По умолчанию — бот; кабинет подменяет command в docker-compose.yml.
CMD ["python", "-m", "bot"]
