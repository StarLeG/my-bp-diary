# QA-стенд (SPEC.md, §22): E2E-тесты бота (Telethon) и Web UI (Playwright),
# нагрузочные тесты qa/.
#
# Сборка — из корня проекта (контекст — корень):
#   docker build -f qa/Dockerfile -t my-bp-diary-qa .
FROM python:3.11-slim

WORKDIR /app

# uv — как в основном Dockerfile; зависимости — только из pyproject.toml/uv.lock
# (группы qa: Telethon + pytest, load: Locust, pytest-html, Playwright).
RUN pip install --no-cache-dir uv
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_HTTP_TIMEOUT=120 \
    PATH="/app/.venv/bin:$PATH"

# README.md нужен hatchling'у (readme в pyproject.toml) при сборке пакета.
COPY pyproject.toml uv.lock README.md ./
COPY bot ./bot
COPY web ./web
RUN uv sync --frozen --group qa --group load \
    && python -m playwright install --with-deps chromium

# Тесты и конфигурация стенда; qa/reports — том: скриншоты, HTML-отчёт,
# сессия Telethon (*.session — в .gitignore).
COPY qa ./qa

CMD ["pytest", "qa/", "--html=qa/reports/report.html", "--self-contained-html", "-q", "-s"]
