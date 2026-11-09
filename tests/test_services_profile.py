"""Тесты данных пациента для врача (SPEC.md, §6.12).

Покрывается: проверка ФИО и даты рождения (формат, диапазон, граница 16
лет в часовом поясе пациента), сохранение и удаление, имя из Telegram,
П7 (кому и один раз), подписи для врача, склонение возраста, сортировка
и имя файла латиницей.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from bot import schemas
from bot.models import Doctor, DoctorPatient, User
from bot.services import profile

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def person(**overrides) -> User:
    values = dict(
        telegram_id=111_111,
        timezone="Europe/Minsk",
        last_name="Иванов",
        first_name="Иван",
        middle_name="Иванович",
        birth_date=date(1958, 3, 12),
        sex="m",
    )
    values.update(overrides)
    return User(**values)


class TestValidation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Иванов", "Иванов"),
            ("  Салтыков-Щедрин ", "Салтыков-Щедрин"),
            ("д’Артаньян", "д’Артаньян"),
            ("О'Нил", "О'Нил"),
            ("Мария  Анна", "Мария Анна"),
            ("Салтыков - Щедрин", "Салтыков-Щедрин"),
            ("Ivanov", "Ivanov"),
        ],
    )
    def test_names_ok(self, raw: str, expected: str) -> None:
        assert schemas.validate_person_name(raw) == expected

    @pytest.mark.parametrize("raw", ["", "  ", "Ив4н", "-Иван", "Иван-", "Иван_", "А" * 51, "Иван!"])
    def test_names_bad(self, raw: str) -> None:
        with pytest.raises(ValueError):
            schemas.validate_person_name(raw)

    def test_birth_ok(self) -> None:
        assert schemas.parse_birth_date("12.03.1958", date(2026, 11, 9)) == date(1958, 3, 12)
        assert schemas.parse_birth_date(" 1.2.1990 ", date(2026, 11, 9)) == date(1990, 2, 1)

    @pytest.mark.parametrize("raw", ["1958-03-12", "31.02.1990", "12.03.1899", "10.11.2026", "abc"])
    def test_birth_bad(self, raw: str) -> None:
        with pytest.raises(ValueError) as info:
            schemas.parse_birth_date(raw, date(2026, 11, 9))
        assert not isinstance(info.value, schemas.TooYoungError)

    def test_sixteen_boundary(self) -> None:
        today = date(2026, 11, 9)
        assert schemas.parse_birth_date("09.11.2010", today) == date(2010, 11, 9)
        with pytest.raises(schemas.TooYoungError):
            schemas.parse_birth_date("10.11.2010", today)

    def test_today_in_patient_timezone(self) -> None:
        # 22:30 UTC 9 ноября — в Минске уже 10 ноября
        user = person(timezone="Europe/Minsk")
        assert profile.today_for(user, datetime(2026, 11, 9, 22, 30, tzinfo=timezone.utc)) == date(2026, 11, 10)

    def test_validate_bundle(self) -> None:
        data = profile.validate(" Иванов ", "Иван", None, date(1958, 3, 12), "m", date(2026, 11, 9))
        assert (data.last_name, data.middle_name, data.sex) == ("Иванов", None, "m")
        with pytest.raises(ValueError):
            profile.validate("Иванов", "Иван", None, date(1958, 3, 12), "x", date(2026, 11, 9))
        with pytest.raises(schemas.TooYoungError):
            profile.validate("Иванов", "Иван", None, date(2015, 1, 1), "m", date(2026, 11, 9))


class TestStorage:
    async def test_save_and_clear(self, session: AsyncSession, user: User) -> None:
        assert not profile.has_profile(user)
        data = profile.validate("Иванов", "Иван", "Иванович", date(1958, 3, 12), "f", date(2026, 11, 9))
        await profile.save(session, user, data)
        await session.refresh(user)
        assert profile.has_profile(user) and user.sex == "f"
        assert await profile.count_filled(session) == 1
        await profile.clear(session, user)
        await session.refresh(user)
        assert not profile.has_profile(user) and user.middle_name is None
        assert await profile.count_filled(session) == 0

    async def test_touch_telegram_name(self, session: AsyncSession, user: User) -> None:
        assert await profile.touch_telegram_name(session, user.telegram_id, "Анна", "Иванова")
        await session.refresh(user)
        assert user.telegram_name == "Анна Иванова"
        assert not await profile.touch_telegram_name(session, user.telegram_id, "Анна", "Иванова")
        assert not await profile.touch_telegram_name(session, user.telegram_id, None, None)
        assert not await profile.touch_telegram_name(session, 999, "Кто", "То")  # не пациент
        long_name = "Я" * 100
        assert await profile.touch_telegram_name(session, user.telegram_id, long_name, None)
        await session.refresh(user)
        assert len(user.telegram_name) == 64

    async def test_needs_prompt(self, session: AsyncSession, user: User) -> None:
        assert not await profile.needs_prompt(session, user)  # врача нет
        doctor = Doctor(email="d@example.org", name="Врач", password_hash="x")
        session.add(doctor)
        await session.commit()
        session.add(DoctorPatient(doctor_id=doctor.id, user_id=user.id, status="pending"))
        await session.commit()
        assert await profile.needs_prompt(session, user)
        await profile.mark_prompted(session, user, NOW)
        assert user.profile_prompted_at == NOW
        assert not await profile.needs_prompt(session, user)  # один раз

    async def test_no_prompt_with_profile(self, session: AsyncSession) -> None:
        filled = person()
        session.add(filled)
        await session.commit()
        assert not await profile.needs_prompt(session, filled)


class TestLabels:
    @pytest.mark.parametrize(
        ("years", "text"),
        [(1, "1 год"), (2, "2 года"), (5, "5 лет"), (11, "11 лет"), (14, "14 лет"),
         (21, "21 год"), (22, "22 года"), (41, "41 год"), (67, "67 лет"), (111, "111 лет")],
    )
    def test_years_word(self, years: int, text: str) -> None:
        assert profile.years_word(years) == text

    def test_filled_labels(self) -> None:
        user = person()
        assert profile.full_name(user) == "Иванов Иван Иванович"
        assert profile.short_name(user) == "Иванов И. И."
        assert profile.age_text(user, NOW) == "68 лет"
        assert profile.list_label(user, NOW) == "Иванов Иван Иванович · 12.03.1958 (68 лет) · м"
        assert profile.card_title(user, NOW) == "Иванов Иван Иванович, 68 лет"
        assert profile.card_details(user) == "Дата рождения: 12.03.1958 · Пол: мужской"
        assert profile.bot_label(user, NOW) == "Иванов И. И., 68 лет"
        assert profile.notify_name(user, "Ваня") == "Иванов И. И."
        assert profile.report_subject(user, NOW) == "Иванов Иван Иванович, 12.03.1958 (68 лет)"

    def test_without_middle_name(self) -> None:
        user = person(middle_name=None, sex="f")
        assert profile.full_name(user) == "Иванов Иван"
        assert profile.short_name(user) == "Иванов И."
        assert profile.card_details(user).endswith("Пол: женский")

    def test_empty_labels(self) -> None:
        anon = User(telegram_id=555, timezone="Europe/Minsk")
        named = User(telegram_id=556, timezone="Europe/Minsk", telegram_name="Анна Иванова")
        assert profile.list_label(anon) == "Пациент 555 — данные не заполнены"
        assert profile.list_label(named) == "Анна Иванова — данные не заполнены"
        assert profile.card_title(named) == "Анна Иванова"
        assert profile.card_details(named) == "Данные не заполнены"
        assert profile.bot_label(anon) == "Пациент 555"
        assert profile.notify_name(named, "Аня") == "Аня"
        assert profile.notify_name(anon, None) == "Пациент 555"
        assert profile.report_subject(anon) is None
        assert profile.file_base(anon) is None

    def test_sorted_patients(self) -> None:
        rows = [
            ("link", User(telegram_id=1, timezone="Europe/Minsk")),
            ("link", person(last_name="Яковлев")),
            ("link", person(last_name="абрамов")),
        ]
        ordered = [row[1].last_name for row in profile.sorted_patients(rows)]
        assert ordered == ["абрамов", "Яковлев", None]


class TestFileName:
    @pytest.mark.parametrize(
        ("text", "latin"),
        [("Иванов", "Ivanov"), ("Щукина", "Shhukina"), ("Цой", "Czoj"), ("Ёлкин", "Yolkin"),
         ("Салтыков-Щедрин", "Saltykov_Shhedrin"), ("Smith", "Smith"), ("Підъячий", "Pidyachij")],
    )
    def test_transliterate(self, text: str, latin: str) -> None:
        assert profile.transliterate(text) == latin

    def test_file_base(self) -> None:
        assert profile.file_base(person()) == "Ivanov_I_I"
        assert profile.file_base(person(middle_name=None)) == "Ivanov_I"
        assert profile.file_base(person(last_name="Ъ", first_name="Ь", middle_name=None)) is None
