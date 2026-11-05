# Развёртывание «Дневника гипертоника» в Docker (SPEC.md, раздел 16.3).
#
#   .\deploy.ps1          — собрать образ и запустить бота и личный кабинет
#   .\deploy.ps1 https    — то же + HTTPS (Caddy с Let's Encrypt)
#   .\deploy.ps1 logs     — логи всех сервисов
#   .\deploy.ps1 ps       — состояние контейнеров
#   .\deploy.ps1 down     — остановить и убрать контейнеры
#
# Секреты (BOT_TOKEN, WEB_SECRET_KEY) задаются только в .env — файл не попадает в git.
param(
    [ValidateSet("up", "https", "logs", "ps", "down")]
    [string]$Command = "up"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "docker не найден. Установите Docker и повторите запуск."
}

# Вызов docker compose: прогресс сборки пишется в stderr и не должен
# ронять скрипт при $ErrorActionPreference = "Stop" (PowerShell 5.1).
function Invoke-Compose {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$ComposeArgs)
    $ErrorActionPreference = "Continue"
    & docker compose @ComposeArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Error "docker compose $($ComposeArgs -join ' ') завершился с кодом $LASTEXITCODE."
    }
}

$ErrorActionPreference = "Continue"
docker compose version *> $null
$versionExit = $LASTEXITCODE
$ErrorActionPreference = "Stop"
if ($versionExit -ne 0) {
    Write-Error "Плагин docker compose не найден (нужен Compose V2)."
}

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Файл .env не найден — создан из .env.example."
    Write-Host "Заполните BOT_TOKEN и WEB_SECRET_KEY (и CABINET_URL, если развёрнут кабинет),"
    Write-Host "затем запустите .\deploy.ps1 снова."
    exit 1
}

# Проверка секретов: только факт заполненности, значения не выводятся.
$envContent = Get-Content .env
if (-not ($envContent -match '^BOT_TOKEN=.+')) {
    Write-Error "В .env не задан BOT_TOKEN."
}
if (-not ($envContent -match '^WEB_SECRET_KEY=.+')) {
    Write-Error "В .env не задан WEB_SECRET_KEY (без него личный кабинет не работает)."
}

switch ($Command) {
    "up" {
        Invoke-Compose up -d --build
    }
    "https" {
        if (-not ($envContent -match '^CABINET_URL=https?://.+')) {
            Write-Error "Для HTTPS в .env нужен CABINET_URL (например https://cabinet.example.org)."
        }
        Invoke-Compose --profile https up -d --build
    }
    "logs" {
        Invoke-Compose logs -f --tail=100
    }
    "ps" {
        Invoke-Compose ps
    }
    "down" {
        Invoke-Compose --profile https down
    }
}

Write-Host ""
Invoke-Compose ps
