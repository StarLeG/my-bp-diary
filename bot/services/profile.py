"""Данные пациента для врача: ФИО, дата рождения, пол (SPEC.md, §6.12).

Данные нужны только врачу и заполняются все сразу (кроме отчества) или не
заполняются вовсе. Пока их нет, врач видит имя из Telegram
(`users.telegram_name`) с пометкой «данные не заполнены», а если и его нет —
«Пациент {telegram_id}». Здесь же — подписи пациента для врача (список,
карточка, PDF, бот), склонение возраста и имя файла латиницей.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot import schemas
from bot.models import DoctorPatient, User

#: Пол: значение в БД → подпись (полная, короткая).
SEX_LABELS = {"m": ("мужской", "м"), "f": ("женский", "ж")}

#: Длина `users.telegram_name` (§4.1).
TELEGRAM_NAME_MAX = 64


@dataclass(frozen=True)
class PersonalData:
    """Проверенные данные пациента (§6.12)."""

    last_name: str
    first_name: str
    middle_name: Optional[str]
    birth_date: date
    sex: str


def has_profile(user: User) -> bool:
    """Данные пациента заполнены (фамилия, имя, дата рождения, пол)."""
    return bool(user.last_name and user.first_name and user.birth_date and user.sex)


def today_for(user: User, now: Optional[datetime] = None) -> date:
    """Сегодня пациента — в его часовом поясе (возраст, граница 16 лет)."""
    return (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(user.timezone)).date()


def validate(
    last_name: str,
    first_name: str,
    middle_name: Optional[str],
    birth_date: date,
    sex: str,
    today: date,
) -> PersonalData:
    """Проверить данные из бота или формы ЛК (одни правила, `bot/schemas.py`).

    Исключения: `ValueError` — поле не подходит; `schemas.TooYoungError` —
    младше 16 лет.
    """
    if sex not in SEX_LABELS:
        raise ValueError("пол не распознан")
    return PersonalData(
        last_name=schemas.validate_person_name(last_name),
        first_name=schemas.validate_person_name(first_name),
        middle_name=schemas.validate_person_name(middle_name) if middle_name else None,
        birth_date=schemas.validate_birth_date(birth_date, today),
        sex=sex,
    )


async def save(session: AsyncSession, user: User, data: PersonalData) -> None:
    """Сохранить данные пациента (одной транзакцией)."""
    user.last_name = data.last_name
    user.first_name = data.first_name
    user.middle_name = data.middle_name
    user.birth_date = data.birth_date
    user.sex = data.sex
    await session.commit()


async def clear(session: AsyncSession, user: User) -> None:
    """Удалить данные пациента: врач снова видит имя из Telegram (§6.12)."""
    user.last_name = user.first_name = user.middle_name = None
    user.birth_date = None
    user.sex = None
    await session.commit()


async def touch_telegram_name(
    session: AsyncSession, telegram_id: int, first_name: Optional[str], last_name: Optional[str]
) -> bool:
    """Обновить `telegram_name`, если сменилось (сначала чтение). True — записано.

    Вызывается на каждое обращение к боту: запись (и блокировка SQLite) —
    только у существующего пациента и только при изменении.
    """
    name = " ".join(part for part in (first_name, last_name) if part).strip()[:TELEGRAM_NAME_MAX]
    if not name:
        return False
    current = (
        await session.execute(
            select(User.id, User.telegram_name).where(User.telegram_id == telegram_id)
        )
    ).first()
    if current is None or current.telegram_name == name:
        return False
    await session.execute(update(User).where(User.id == current.id).values(telegram_name=name))
    await session.commit()
    return True


# ─────────────────────────── Предложение заполнить (П7) ───────────────────────────


async def needs_prompt(session: AsyncSession, user: User) -> bool:
    """Показать П7: врач `pending` / `active`, данных нет, ещё не предлагали."""
    if has_profile(user) or user.profile_prompted_at is not None:
        return False
    found = (
        await session.execute(
            select(DoctorPatient.id).where(
                DoctorPatient.user_id == user.id,
                DoctorPatient.status.in_(("pending", "active")),
            )
        )
    ).first()
    return found is not None


async def mark_prompted(session: AsyncSession, user: User, now: Optional[datetime] = None) -> None:
    """П7 показано — больше не предлагать."""
    user.profile_prompted_at = now or datetime.now(timezone.utc)
    await session.commit()


async def count_filled(session: AsyncSession) -> int:
    """Сколько пациентов заполнили данные (админка, §18.2) — только счётчик."""
    return int(
        (
            await session.execute(
                select(func.count(User.id)).where(
                    User.last_name.is_not(None),
                    User.first_name.is_not(None),
                    User.birth_date.is_not(None),
                    User.sex.is_not(None),
                )
            )
        ).scalar_one()
    )


# ─────────────────────────── Подписи (§6.12) ───────────────────────────


def years_word(years: int) -> str:
    """«67 лет» / «41 год» / «22 года» — склонение по последним цифрам."""
    last_two, last = years % 100, years % 10
    if 11 <= last_two <= 14:
        word = "лет"
    elif last == 1:
        word = "год"
    elif 2 <= last <= 4:
        word = "года"
    else:
        word = "лет"
    return f"{years} {word}"


def full_name(user: User) -> str:
    """«Иванов Иван Иванович» (без отчества — «Иванов Иван»)."""
    return " ".join(part for part in (user.last_name, user.first_name, user.middle_name) if part)


def short_name(user: User) -> str:
    """«Иванов И. И.» (без отчества — «Иванов И.»)."""
    initials = " ".join(f"{part[0]}." for part in (user.first_name, user.middle_name) if part)
    return f"{user.last_name} {initials}"


def age_text(user: User, now: Optional[datetime] = None) -> str:
    """Возраст пациента на сегодня: «67 лет»."""
    return years_word(schemas.age_on(user.birth_date, today_for(user, now)))  # type: ignore[arg-type]


def fallback_name(user: User) -> str:
    """Без данных: имя из Telegram или «Пациент {telegram_id}»."""
    return user.telegram_name or f"Пациент {user.telegram_id}"


def list_label(user: User, now: Optional[datetime] = None) -> str:
    """Строка списка пациентов врача (кабинет)."""
    if not has_profile(user):
        return f"{fallback_name(user)} — данные не заполнены"
    sex = SEX_LABELS[user.sex][1]  # type: ignore[index]
    return f"{full_name(user)} · {user.birth_date:%d.%m.%Y} ({age_text(user, now)}) · {sex}"


def card_title(user: User, now: Optional[datetime] = None) -> str:
    """Начало заголовка карточки пациента: «Иванов Иван Иванович, 67 лет»."""
    if not has_profile(user):
        return fallback_name(user)
    return f"{full_name(user)}, {age_text(user, now)}"


def card_details(user: User) -> str:
    """Строка под заголовком карточки: дата рождения и пол / «Данные не заполнены»."""
    if not has_profile(user):
        return "Данные не заполнены"
    return f"Дата рождения: {user.birth_date:%d.%m.%Y} · Пол: {SEX_LABELS[user.sex][0]}"  # type: ignore[index]


def bot_label(user: User, now: Optional[datetime] = None) -> str:
    """Кнопка «Мои пациенты» в боте врача: «Иванов И. И., 67 лет»."""
    if not has_profile(user):
        return fallback_name(user)
    return f"{short_name(user)}, {age_text(user, now)}"


def notify_name(user: User, telegram_first_name: Optional[str]) -> str:
    """`{name}` в уведомлениях врачу: «Иванов И. И.» или имя из Telegram."""
    if has_profile(user):
        return short_name(user)
    return telegram_first_name or fallback_name(user)


def report_subject(user: User, now: Optional[datetime] = None) -> Optional[str]:
    """Подпись в шапке PDF: «Иванов Иван Иванович, 12.03.1958 (67 лет)» (§10.1).

    None — данных нет: вызывающий оставляет прежнюю подпись.
    """
    if not has_profile(user):
        return None
    return f"{full_name(user)}, {user.birth_date:%d.%m.%Y} ({age_text(user, now)})"


def sorted_patients(rows, now: Optional[datetime] = None):
    """Пациенты врача по фамилии; без данных — в конце, в прежнем порядке."""
    return sorted(
        rows,
        key=lambda row: (
            not has_profile(row[1]),
            (row[1].last_name or "").casefold(),
            (row[1].first_name or "").casefold(),
        ),
    )


# ─────────────────────────── Имя файла латиницей (§10.1) ───────────────────────────

#: ГОСТ 7.79-2000, система Б (без апострофов и диакритики).
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh",
    "з": "z", "и": "i", "й": "j", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "x", "ц": "cz",
    "ч": "ch", "ш": "sh", "щ": "shh", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", "і": "i", "ў": "u",
}


def transliterate(text: str) -> str:
    """Кириллица → латиница (ГОСТ 7.79-2000 Б); прочие буквы — как есть, остальное — `_`."""
    out: list[str] = []
    for char in text:
        lower = char.lower()
        if lower in _TRANSLIT:
            latin = _TRANSLIT[lower]
            out.append(latin.capitalize() if char != lower and latin else latin)
        elif char.isascii() and char.isalnum():
            out.append(char)
        else:
            out.append("_")
    return "".join(out)


def file_base(user: User) -> Optional[str]:
    """`Ivanov_I_I` для имени файла врача; None — данных нет."""
    if not has_profile(user):
        return None
    parts = [transliterate(user.last_name)]  # type: ignore[arg-type]
    parts += [transliterate(part[0]) for part in (user.first_name, user.middle_name) if part]
    base = "_".join(part.strip("_") for part in parts if part.strip("_"))
    return base or None
