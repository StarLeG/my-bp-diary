"""Административная панель (SPEC.md, §18).

Команда `/admin` и экраны `adm:*`: статистика БД, бэкапы, здоровье системы,
рассылка, поиск пользователя. Доступ — только по `AdminFilter` (ID из
`ADMIN_TELEGRAM_IDS`); чужие запросы получают «Доступ запрещен». Все действия
администратора логируются. Личные медицинские данные не показываются (§18):
только агрегаты и техническая информация.

`AdminFilter` проверяет ID на каждый вызов (окружение может меняться в тестах),
поэтому роутер можно подключать условно — см. `bot/handlers/__init__.py`.
"""
from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.exceptions import TelegramAPIError

from bot import keyboards, texts
from bot.config import admin_ids_from_env
from bot.services.admin import AdminService, DbStats, SystemHealth, UserCard, broadcast_message
from bot.services.backup import MAX_SEND_BYTES, BackupError, BackupService

logger = logging.getLogger(__name__)

router = Router(name="admin")

#: Ключ FSM-data: черновик текста рассылки.
B_TEXT = "bcast_text"


class AdminFilter(BaseFilter):
    """Пропускает только ID из `ADMIN_TELEGRAM_IDS` (SPEC.md, §18).

    Список читается на каждый вызов: тесты и переизменение окружения не
    требуют пересоздания фильтра.
    """

    async def __call__(self, event) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id in admin_ids_from_env()


admin_only = AdminFilter()


class AdminStates(StatesGroup):
    """FSM-состояния свободного ввода админки."""

    broadcast = State()
    search = State()


# ─────────────────────────── Экраны ───────────────────────────


def _menu_screen() -> tuple[str, object]:
    return texts.ADMIN_MENU, keyboards.admin_menu_kb()


def _fmt_stats(stats: DbStats) -> str:
    lines = [
        texts.ADMIN_STATS_HEADER,
        texts.ADMIN_STATS_USERS.format(count=stats.users),
        texts.ADMIN_STATS_DAU.format(count=stats.dau),
        texts.ADMIN_STATS_WAU.format(count=stats.wau),
        texts.ADMIN_STATS_MEASUREMENTS.format(count=stats.measurements),
        texts.ADMIN_STATS_MEDICATIONS.format(count=stats.medications),
        texts.ADMIN_STATS_DB_SIZE.format(
            size=texts.fmt_size(stats.db_size), wal=texts.fmt_size(stats.wal_size)
        ),
        texts.ADMIN_STATS_SLOTS.format(
            morning=stats.slots_enabled.get("morning", 0),
            noon=stats.slots_enabled.get("noon", 0),
            evening=stats.slots_enabled.get("evening", 0),
        ),
        texts.ADMIN_STATS_CACHE_NOTE,
    ]
    return "\n".join(lines)


def _fmt_health(health: SystemHealth) -> str:
    status = (
        texts.ADMIN_HEALTH_SCHEDULER_RUNNING
        if health.scheduler_running
        else texts.ADMIN_HEALTH_SCHEDULER_STOPPED
    )
    lines = [
        texts.ADMIN_HEALTH_HEADER,
        texts.ADMIN_HEALTH_UPTIME.format(uptime=texts.fmt_uptime(health.uptime_seconds)),
        texts.ADMIN_HEALTH_VERSION.format(
            version=health.app_version, python=health.python_version
        ),
        texts.ADMIN_HEALTH_SCHEDULER.format(status=status, jobs=health.jobs_count),
        texts.ADMIN_HEALTH_ERRORS.format(count=health.errors_24h),
    ]
    return "\n".join(lines)


def _fmt_backup_screen(backup: BackupService) -> str:
    lines = [texts.ADMIN_BACKUP_HEADER]
    status = backup.last_status()
    if status is None:
        lines.append(texts.ADMIN_BACKUP_NONE)
    else:
        lines.append(
            texts.ADMIN_BACKUP_LAST.format(
                name=status.name,
                created=status.created_at.strftime("%d.%m.%Y %H:%M"),
                size=texts.fmt_size(status.size_bytes),
                integrity=texts.fmt_backup_status(status.integrity_ok),
            )
        )
        if status.uploaded_url:
            lines.append(texts.ADMIN_BACKUP_OFFSITE_OK.format(url=status.uploaded_url))
        elif status.upload_error:
            lines.append(
                texts.ADMIN_BACKUP_OFFSITE_FAIL.format(error=status.upload_error)
            )
        else:
            lines.append(texts.ADMIN_BACKUP_OFFSITE_NONE)
    files = backup.list_backups()[:7]
    if files:
        lines.append(texts.ADMIN_BACKUP_LIST_HEADER)
        lines.extend(
            texts.ADMIN_BACKUP_ITEM.format(
                name=info.name, size=texts.fmt_size(info.size_bytes)
            )
            for info in files
        )
    return "\n".join(lines)


def _fmt_user_card(card: UserCard) -> str:
    created = card.created_at.strftime("%d.%m.%Y") if card.created_at else "—"
    if card.reminder_slots:
        slots = ", ".join(
            texts.SLOT_LABELS.get(slot, slot) for slot in card.reminder_slots
        )
        reminders = texts.ADMIN_SEARCH_REMINDERS.format(slots=slots)
    else:
        reminders = texts.ADMIN_SEARCH_REMINDERS_OFF
    return texts.ADMIN_SEARCH_CARD.format(
        telegram_id=card.telegram_id,
        created=created,
        tz=card.timezone,
        count=card.measurements_count,
        onboarded=(
            texts.ADMIN_SEARCH_ONBOARDED if card.onboarded else texts.ADMIN_SEARCH_NOT_ONBOARDED
        ),
        reminders=reminders,
    )


async def _show(message: Message, text: str, markup: object) -> None:
    await message.edit_text(text, reply_markup=markup)  # type: ignore[arg-type]


# ─────────────────────────── Меню и доступ ───────────────────────────


@router.message(Command("admin"), admin_only)
async def cmd_admin(message: Message, state: FSMContext) -> None:
    """`/admin` — главное меню админки (§18.1)."""
    await state.clear()
    logger.info("Админка: %s открыл меню", message.from_user.id)  # type: ignore[union-attr]
    text, markup = _menu_screen()
    await message.answer(text, reply_markup=markup)


@router.message(Command("admin"))
async def cmd_admin_denied(message: Message) -> None:
    """`/admin` от не-админа — «Доступ запрещен» (§18, дословно)."""
    logger.warning("Админка: отказ %s", message.from_user.id)  # type: ignore[union-attr]
    await message.answer(texts.ADMIN_FORBIDDEN)


@router.callback_query(F.data == "adm:back", admin_only)
async def cb_admin_back(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, markup = _menu_screen()
    await _show(callback.message, text, markup)  # type: ignore[arg-type]
    await callback.answer()


# ─────────────────────────── Статистика БД (§18.2) ───────────────────────────


@router.callback_query(F.data.in_({"adm:stats", "adm:refresh"}), admin_only)
async def cb_admin_stats(
    callback: CallbackQuery, admin: AdminService
) -> None:
    """Статистика БД; `adm:refresh` — пересчитать, минуя кэш 5 минут."""
    force = callback.data == "adm:refresh"  # type: ignore[union-attr]
    stats = await admin.get_stats(force=force)
    await _show(callback.message, _fmt_stats(stats), keyboards.admin_stats_kb())  # type: ignore[arg-type]
    await callback.answer()


# ─────────────────────────── Здоровье системы (§18.4) ───────────────────────────


@router.callback_query(F.data == "adm:health", admin_only)
async def cb_admin_health(callback: CallbackQuery, admin: AdminService) -> None:
    health = admin.get_health()
    await _show(callback.message, _fmt_health(health), keyboards.admin_health_kb())  # type: ignore[arg-type]
    await callback.answer()


# ─────────────────────────── Бэкапы (§18.3) ───────────────────────────


@router.callback_query(F.data == "adm:backups", admin_only)
async def cb_admin_backups(callback: CallbackQuery, backup: BackupService) -> None:
    await _show(callback.message, _fmt_backup_screen(backup), keyboards.admin_backups_kb())  # type: ignore[arg-type]
    await callback.answer()


@router.callback_query(F.data == "adm:backup:create", admin_only)
async def cb_admin_backup_create(
    callback: CallbackQuery, backup: BackupService
) -> None:
    """«Создать бэкап сейчас» (§18.3): по завершении — отчёт в чат."""
    logger.info("Админка: %s запустил бэкап", callback.from_user.id)
    await callback.answer(texts.ADMIN_BACKUP_CREATING)
    try:
        result = await backup.create_backup()
    except BackupError as exc:
        logger.warning("Админка: бэкап не удался: %s", exc)
        await callback.message.answer(  # type: ignore[union-attr]
            texts.ADMIN_BACKUP_ERROR.format(error=exc),
            reply_markup=keyboards.admin_backups_kb(),
        )
        return
    report = "\n".join(
        [
            texts.ADMIN_BACKUP_DONE.format(
                name=result.name, size=texts.fmt_size(result.size_bytes)
            ),
            texts.fmt_backup_status(result.integrity_ok),
        ]
    )
    await callback.message.answer(report, reply_markup=keyboards.admin_backups_kb())  # type: ignore[union-attr]


@router.callback_query(F.data == "adm:backup:download", admin_only)
async def cb_admin_backup_download(
    callback: CallbackQuery, backup: BackupService
) -> None:
    """«Скачать последний бэкап» (§18.3): файл < 50 МБ — в чат, иначе — S3."""
    logger.info("Админка: %s запросил скачивание бэкапа", callback.from_user.id)
    files = backup.list_backups()
    if not files:
        await callback.answer(texts.ADMIN_BACKUP_NONE, show_alert=True)
        return
    latest = files[0]
    path = backup.backup_dir / latest.name
    if latest.size_bytes < MAX_SEND_BYTES:
        await callback.message.answer_document(  # type: ignore[union-attr]
            FSInputFile(path), caption=latest.name
        )
        await callback.answer()
        return
    url = backup.remote_url(latest.name)
    if url:
        text = texts.ADMIN_BACKUP_TOO_BIG.format(size=texts.fmt_size(latest.size_bytes), url=url)
    else:
        text = texts.ADMIN_BACKUP_NO_S3.format(size=texts.fmt_size(latest.size_bytes))
    await callback.answer(text, show_alert=True)


# ─────────────────────────── Рассылка (§18.5) ───────────────────────────


@router.callback_query(F.data == "adm:bcast", admin_only)
async def cb_admin_bcast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.broadcast)
    await _show(
        callback.message,
        texts.ADMIN_BCAST_PROMPT,
        keyboards.input_cancel_kb("adm:bcast:cancel"),
    )  # type: ignore[arg-type]
    await callback.answer()


@router.message(AdminStates.broadcast)
async def msg_admin_bcast(message: Message, state: FSMContext) -> None:
    """Текст рассылки → превью с «Подтвердить отправку» (§18.5)."""
    text = (message.text or "").strip()
    if not text:
        await message.answer(
            texts.ADMIN_BCAST_EMPTY,
            reply_markup=keyboards.input_cancel_kb("adm:bcast:cancel"),
        )
        return
    await state.update_data(**{B_TEXT: text})
    await message.answer(
        texts.ADMIN_BCAST_PREVIEW.format(text=text),
        reply_markup=keyboards.admin_broadcast_preview_kb(),
    )


@router.callback_query(F.data == "adm:bcast:cancel", admin_only)
async def cb_admin_bcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer(texts.ADMIN_BCAST_CANCELLED)
    text, markup = _menu_screen()
    await _show(callback.message, text, markup)  # type: ignore[arg-type]


@router.callback_query(F.data == "adm:bcast:confirm", admin_only)
async def cb_admin_bcast_confirm(
    callback: CallbackQuery, state: FSMContext, bot: Bot, admin: AdminService
) -> None:
    """«Подтвердить отправку»: прогресс «Отправлено X из Y...», итог — отчёт."""
    data = await state.get_data()
    text = data.get(B_TEXT)
    await state.clear()
    if not text:  # устаревшие кнопки без черновика
        menu_text, markup = _menu_screen()
        await _show(callback.message, menu_text, markup)  # type: ignore[arg-type]
        await callback.answer()
        return
    user_ids = await admin.all_user_ids()
    if not user_ids:
        await _show(callback.message, texts.ADMIN_BCAST_NO_USERS, keyboards.admin_menu_kb())  # type: ignore[arg-type]
        await callback.answer()
        return
    logger.info("Админка: %s запустил рассылку на %s", callback.from_user.id, len(user_ids))
    progress = callback.message

    async def on_progress(done: int, total: int) -> None:
        await progress.edit_text(texts.ADMIN_BCAST_PROGRESS.format(done=done, total=total))  # type: ignore[union-attr]

    await progress.edit_text(  # type: ignore[union-attr]
        texts.ADMIN_BCAST_PROGRESS.format(done=0, total=len(user_ids))
    )
    result = await broadcast_message(bot, user_ids, text, on_progress)
    await progress.edit_text(  # type: ignore[union-attr]
        texts.ADMIN_BCAST_REPORT.format(
            delivered=result.delivered, blocked=result.blocked
        ),
        reply_markup=keyboards.admin_menu_kb(),
    )
    await callback.answer()


# ─────────────────────────── Поиск пользователя (§18.6) ───────────────────────────


@router.callback_query(F.data == "adm:search", admin_only)
async def cb_admin_search(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.search)
    await _show(
        callback.message,
        texts.ADMIN_SEARCH_PROMPT,
        keyboards.input_cancel_kb("adm:search"),
    )  # type: ignore[arg-type]
    await callback.answer()


@router.message(AdminStates.search)
async def msg_admin_search(
    message: Message, state: FSMContext, admin: AdminService
) -> None:
    """Ввод `telegram_id`; карточка — только агрегаты (§18.6)."""
    raw = (message.text or "").strip()
    if raw.startswith("@"):
        await message.answer(
            texts.ADMIN_SEARCH_USERNAME_UNSUPPORTED,
            reply_markup=keyboards.input_cancel_kb("adm:search"),
        )
        return
    if not raw.lstrip("-").isdigit():
        await message.answer(
            texts.ADMIN_SEARCH_BAD_INPUT,
            reply_markup=keyboards.input_cancel_kb("adm:search"),
        )
        return
    telegram_id = int(raw)
    card = await admin.find_user(telegram_id)
    if card is None:
        await message.answer(
            texts.ADMIN_SEARCH_NOT_FOUND,
            reply_markup=keyboards.input_cancel_kb("adm:search"),
        )
        return
    logger.info("Админка: %s искал пользователя %s", message.from_user.id, telegram_id)  # type: ignore[union-attr]
    await message.answer(
        _fmt_user_card(card), reply_markup=keyboards.admin_search_card_kb(telegram_id)
    )


@router.callback_query(F.data.startswith("adm:test:"), admin_only)
async def cb_admin_test_message(callback: CallbackQuery, bot: Bot) -> None:
    """«Отправить тестовое сообщение» (§18.6): проверка доставки уведомлений."""
    value = callback.data.split(":", 2)[2]  # type: ignore[union-attr]
    if not value.isdigit():
        await callback.answer()
        return
    logger.info("Админка: %s — тестовое сообщение %s", callback.from_user.id, value)
    try:
        await bot.send_message(int(value), texts.ADMIN_SEARCH_TEST_MESSAGE)
    except TelegramAPIError:
        logger.warning("Админка: тестовое сообщение %s не доставлено", value)
        await callback.answer(texts.ADMIN_SEARCH_TEST_FAILED, show_alert=True)
        return
    await callback.answer(texts.ADMIN_SEARCH_TEST_SENT)


# ─────────────────────────── Отказ не-админам ───────────────────────────


@router.callback_query(F.data.startswith("adm:"))
async def cb_admin_denied(callback: CallbackQuery) -> None:
    """Любой `adm:*` от не-админа (в том числе подделанный callback)."""
    logger.warning("Админка: отказ %s на %s", callback.from_user.id, callback.data)
    await callback.answer(texts.ADMIN_FORBIDDEN, show_alert=True)
