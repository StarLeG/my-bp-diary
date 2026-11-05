# QA-стенд (SPEC.md, §22): E2E-тесты бота (Telethon) и Web UI (Playwright).
#
# Сборка — из корня проекта (контекст — корень):
#   docker build -f qa/Dockerfile -t my-bp-diary-qa .
FROM python:3.11-slim

WORKDIR /app

# Зависимости проекта (пакеты bot и web) и QA (Telethon, Playwright, pytest-html).
# README.md нужен hatchling'у (readme в pyproject.toml) при сборке пакета.
COPY pyproject.toml README.md ./
COPY bot ./bot
COPY web ./web
COPY qa/requirements.txt qa/requirements.txt
RUN pip install --no-cache-dir . -r qa/requirements.txt \
    && python -m playwright install --with-deps chromium

# Тесты и конфигурация стенда; qa/reports — том: скриншоты, HTML-отчёт,
# сессия Telethon (*.session — в .gitignore).
COPY qa ./qa

CMD ["pytest", "qa/", "--html=qa/reports/report.html", "--self-contained-html", "-q", "-s"]
