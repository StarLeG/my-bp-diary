"""Сервис связей врача и пациента (SPEC.md, разделы 4.8–4.10, 21).

Покрывается: валидация e-mail, создание врача с Argon2id-паролем, жизненный
цикл связи (`pending` → `active` → `revoked`), заметки врача, PDF-отчёт и
гонки вставок (§7.2): UNIQUE по e-mail врача и паре врач–пациент отсекают
конкурентов, проигравший получает уже существующую строку.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from bot.db import create_engine, create_session_factory, init_db
from bot.models import Doctor, DoctorNote, DoctorPatient, Measurement, User
from bot.services import doctor as doctor_service

TG_ID = 111_111


# ─────────────────────────── Валидация e-mail ───────────────────────────


class TestValidEmail:
    @pytest.mark.parametrize(
        ("email", "expected"),
        [
            ("doc@example.org", True),
            ("doctor.name+tag@mail.example", True),
            ("doc@example", False),
            ("doc example.org", False),
            ("doc@", False),
            ("", False),
        ],
    )
    def test_format(self, email: str, expected: bool) -> None:
        assert doctor_service.valid_email(email) is expected

    def test_too_long(self) -> None:
        assert doctor_service.valid_email("a" * 246 + "@example.org") is False

    def test_boundary_length(self) -> None:
        assert doctor_service.valid_email("a" * 243 + "@example.org") is True


# ─────────────────────────── Врачи ───────────────────────────


class TestFindOrCreateDoctor:
    async def test_creates_doctor(self, session: AsyncSession) -> None:
        doctor, created = await doctor_service.find_or_create_doctor(
            session, "doc@example.org"
        )
        assert created is True
        assert doctor.email == "doc@example.org"
        assert doctor.name == "doc"
        assert doctor.password_hash.startswith("$argon2id$")
        assert "token" not in doctor.password_hash  # пароль не в открытом виде

    async def test_email_is_normalized(self, session: AsyncSession) -> None:
        doctor, _ = await doctor_service.find_or_create_doctor(
            session, "  Doc@Example.ORG "
        )
        assert doctor.email == "doc@example.org"
        assert doctor.name == "doc"

    async def test_name_is_local_part_capped(self, session: AsyncSession) -> None:
        local = "x" * 120
        doctor, _ = await doctor_service.find_or_create_doctor(
            session, f"{local}@example.org"
        )
        assert doctor.name == local[:100]

    async def test_second_call_returns_existing(self, session: AsyncSession) -> None:
        first, created_first = await doctor_service.find_or_create_doctor(
            session, "doc@example.org"
        )
        second, created_second = await doctor_service.find_or_create_doctor(
            session, "doc@example.org"
        )
        assert created_first is True
        assert created_second is False
        assert second.id == first.id

    async def test_different_emails_create_two_doctors(self, session: AsyncSession) -> None:
        await doctor_service.find_or_create_doctor(session, "a@example.org")
        await doctor_service.find_or_create_doctor(session, "b@example.org")
        rows = (await session.execute(select(func.count()).select_from(Doctor))).scalar_one()
        assert rows == 2

    async def test_integrity_error_refetches_existing(
        self, session: AsyncSession, mocker
    ) -> None:
        """UNIQUE по e-mail ловится на flush: проигравший получает врача конкурента.

        Первый SELECT подменяется пустым результатом (будто конкурент ещё не
        закоммитил), INSERT падает с `IntegrityError`, повторный SELECT
        возвращает уже существующую запись (§7.2).
        """
        existing, _ = await doctor_service.find_or_create_doctor(session, "doc@example.org")
        real_execute = session.execute
        calls = {"n": 0}

        class _EmptyResult:
            def scalar_one_or_none(self) -> None:
                return None

        async def fake_execute(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _EmptyResult()
            return await real_execute(*args, **kwargs)

        mocker.patch.object(session, "execute", new=fake_execute)
        doctor, created = await doctor_service.find_or_create_doctor(session, "doc@example.org")
        assert created is False
        assert doctor is existing


# ─────────────────────────── Связи врач–пациент ───────────────────────────


class TestLinkPatient:
    async def _doctor(self, session: AsyncSession) -> Doctor:
        doctor, _ = await doctor_service.find_or_create_doctor(session, "doc@example.org")
        return doctor

    async def test_new_link_is_pending(self, session: AsyncSession, user: User) -> None:
        doctor = await self._doctor(session)
        link, created = await doctor_service.link_patient(session, doctor.id, user.id)
        assert created is True
        assert link.status == "pending"

    async def test_duplicate_link_is_not_created(
        self, session: AsyncSession, user: User
    ) -> None:
        doctor = await self._doctor(session)
        await doctor_service.link_patient(session, doctor.id, user.id)
        link, created = await doctor_service.link_patient(session, doctor.id, user.id)
        assert created is False
        assert link.status == "pending"

    async def test_revoked_becomes_pending_again(self, session: AsyncSession, user: User) -> None:
        doctor = await self._doctor(session)
        await doctor_service.link_patient(session, doctor.id, user.id)
        await doctor_service.revoke_access(session, user_id=user.id, doctor_id=doctor.id)
        link, created = await doctor_service.link_patient(session, doctor.id, user.id)
        assert created is False
        assert link.status == "pending"

    async def test_active_link_stays_active(self, session: AsyncSession, user: User) -> None:
        doctor = await self._doctor(session)
        await doctor_service.link_patient(session, doctor.id, user.id)
        await doctor_service.activate_link(session, doctor.id, user.id)
        link, created = await doctor_service.link_patient(session, doctor.id, user.id)
        assert created is False
        assert link.status == "active"

    async def test_link_for_pair(self, session: AsyncSession, user: User) -> None:
        doctor = await self._doctor(session)
        assert await doctor_service.link_for_pair(session, doctor.id, user.id) is None
        await doctor_service.link_patient(session, doctor.id, user.id)
        link = await doctor_service.link_for_pair(session, doctor.id, user.id)
        assert link is not None
        assert link.doctor_id == doctor.id
        assert link.user_id == user.id


class TestLinkQueries:
    async def _setup(self, session: AsyncSession) -> tuple[Doctor, User]:
        doctor, _ = await doctor_service.find_or_create_doctor(session, "doc@example.org")
        user = User(telegram_id=TG_ID, timezone="Europe/Minsk")
        session.add(user)
        await session.commit()
        return doctor, user

    async def test_links_for_patient(self, session: AsyncSession) -> None:
        doctor, user = await self._setup(session)
        await doctor_service.link_patient(session, doctor.id, user.id)
        rows = await doctor_service.links_for_patient(session, user.id)
        assert len(rows) == 1
        link, doctor_row = rows[0]
        assert link.user_id == user.id
        assert doctor_row.email == "doc@example.org"

    async def test_links_for_patient_include_revoked(self, session: AsyncSession) -> None:
        doctor, user = await self._setup(session)
        await doctor_service.link_patient(session, doctor.id, user.id)
        await doctor_service.revoke_access(session, user_id=user.id, doctor_id=doctor.id)
        rows = await doctor_service.links_for_patient(session, user.id)
        assert [link.status for link, _ in rows] == ["revoked"]

    async def test_active_links_for_doctor_only_active(self, session: AsyncSession) -> None:
        doctor, user = await self._setup(session)
        await doctor_service.link_patient(session, doctor.id, user.id)
        assert await doctor_service.active_links_for_doctor(session, doctor.id) == []
        await doctor_service.activate_link(session, doctor.id, user.id)
        rows = await doctor_service.active_links_for_doctor(session, doctor.id)
        assert len(rows) == 1
        link, patient = rows[0]
        assert link.status == "active"
        assert patient.telegram_id == TG_ID

    async def test_active_links_hide_revoked(self, session: AsyncSession) -> None:
        doctor, user = await self._setup(session)
        await doctor_service.link_patient(session, doctor.id, user.id)
        await doctor_service.activate_link(session, doctor.id, user.id)
        await doctor_service.revoke_access(session, user_id=user.id, doctor_id=doctor.id)
        assert await doctor_service.active_links_for_doctor(session, doctor.id) == []


class TestRevokeAccess:
    async def _link(self, session: AsyncSession) -> tuple[Doctor, User]:
        doctor, _ = await doctor_service.find_or_create_doctor(session, "doc@example.org")
        user = User(telegram_id=TG_ID, timezone="Europe/Minsk")
        session.add(user)
        await session.commit()
        await doctor_service.link_patient(session, doctor.id, user.id)
        return doctor, user

    async def test_revoke_sets_status(self, session: AsyncSession) -> None:
        doctor, user = await self._link(session)
        assert await doctor_service.revoke_access(
            session, user_id=user.id, doctor_id=doctor.id
        ) is True
        link = await doctor_service.link_for_pair(session, doctor.id, user.id)
        assert link is not None
        assert link.status == "revoked"

    async def test_revoke_is_idempotent(self, session: AsyncSession) -> None:
        doctor, user = await self._link(session)
        await doctor_service.revoke_access(session, user_id=user.id, doctor_id=doctor.id)
        assert await doctor_service.revoke_access(
            session, user_id=user.id, doctor_id=doctor.id
        ) is True
        link = await doctor_service.link_for_pair(session, doctor.id, user.id)
        assert link is not None
        assert link.status == "revoked"

    async def test_revoke_missing_link_is_false(self, session: AsyncSession, user: User) -> None:
        assert await doctor_service.revoke_access(
            session, user_id=user.id, doctor_id=999
        ) is False

    async def test_revoke_checks_owner(self, session: AsyncSession) -> None:
        """Чужая пара «пользователь–врач» не отзывается: проверяется владелец."""
        doctor, user = await self._link(session)
        other = User(telegram_id=222_222, timezone="Europe/Minsk")
        session.add(other)
        await session.commit()
        assert await doctor_service.revoke_access(
            session, user_id=other.id, doctor_id=doctor.id
        ) is False
        link = await doctor_service.link_for_pair(session, doctor.id, user.id)
        assert link is not None
        assert link.status == "pending"


class TestActivateLink:
    async def _link(self, session: AsyncSession, status: str = "pending") -> tuple[Doctor, User]:
        doctor, _ = await doctor_service.find_or_create_doctor(session, "doc@example.org")
        user = User(telegram_id=TG_ID, timezone="Europe/Minsk")
        session.add(user)
        await session.commit()
        await doctor_service.link_patient(session, doctor.id, user.id)
        if status != "pending":
            link = await doctor_service.link_for_pair(session, doctor.id, user.id)
            assert link is not None
            link.status = status
            await session.commit()
        return doctor, user

    async def test_pending_becomes_active(self, session: AsyncSession) -> None:
        doctor, user = await self._link(session)
        assert await doctor_service.activate_link(session, doctor.id, user.id) is True
        link = await doctor_service.link_for_pair(session, doctor.id, user.id)
        assert link is not None
        assert link.status == "active"

    async def test_active_stays_active(self, session: AsyncSession) -> None:
        doctor, user = await self._link(session, status="active")
        assert await doctor_service.activate_link(session, doctor.id, user.id) is True

    async def test_revoked_is_not_activated(self, session: AsyncSession) -> None:
        doctor, user = await self._link(session, status="revoked")
        assert await doctor_service.activate_link(session, doctor.id, user.id) is False
        link = await doctor_service.link_for_pair(session, doctor.id, user.id)
        assert link is not None
        assert link.status == "revoked"

    async def test_missing_link_is_false(self, session: AsyncSession, user: User) -> None:
        assert await doctor_service.activate_link(session, 999, user.id) is False


# ─────────────────────────── Заметки врача ───────────────────────────


class TestNotes:
    async def _pair(self, session: AsyncSession) -> tuple[Doctor, User]:
        doctor, _ = await doctor_service.find_or_create_doctor(session, "doc@example.org")
        user = User(telegram_id=TG_ID, timezone="Europe/Minsk")
        session.add(user)
        await session.commit()
        return doctor, user

    async def test_add_note_commits(self, session: AsyncSession) -> None:
        doctor, user = await self._pair(session)
        note = await doctor_service.add_note(
            session, doctor.id, user.id, "Продолжайте измерения"
        )
        assert note.id is not None
        assert note.text == "Продолжайте измерения"

    async def test_add_note_strips_text(self, session: AsyncSession) -> None:
        doctor, user = await self._pair(session)
        note = await doctor_service.add_note(session, doctor.id, user.id, "  текст  ")
        assert note.text == "текст"

    @pytest.mark.parametrize("text", ["", "   "])
    async def test_add_note_empty_raises(self, session: AsyncSession, text: str) -> None:
        doctor, user = await self._pair(session)
        with pytest.raises(ValueError):
            await doctor_service.add_note(session, doctor.id, user.id, text)

    async def test_add_note_too_long_raises(self, session: AsyncSession) -> None:
        doctor, user = await self._pair(session)
        with pytest.raises(ValueError):
            await doctor_service.add_note(
                session, doctor.id, user.id, "x" * (doctor_service.NOTE_MAX_LENGTH + 1)
            )

    async def test_add_note_boundary_length(self, session: AsyncSession) -> None:
        doctor, user = await self._pair(session)
        note = await doctor_service.add_note(
            session, doctor.id, user.id, "x" * doctor_service.NOTE_MAX_LENGTH
        )
        assert len(note.text) == doctor_service.NOTE_MAX_LENGTH

    async def test_notes_for_patient_newest_first(self, session: AsyncSession) -> None:
        doctor, user = await self._pair(session)
        await doctor_service.add_note(session, doctor.id, user.id, "Первая")
        await doctor_service.add_note(session, doctor.id, user.id, "Вторая")
        rows = await doctor_service.notes_for_patient(session, user.id)
        assert [note.text for note, _ in rows] == ["Вторая", "Первая"]
        assert all(doctor_row.id == doctor.id for _, doctor_row in rows)

    async def test_notes_are_per_patient(self, session: AsyncSession) -> None:
        doctor, user = await self._pair(session)
        other = User(telegram_id=222_222, timezone="Europe/Minsk")
        session.add(other)
        await session.commit()
        await doctor_service.add_note(session, doctor.id, user.id, "Пациенту 111111")
        assert await doctor_service.notes_for_patient(session, other.id) == []


# ─────────────────────────── PDF-отчёт ───────────────────────────


def _measurement(**overrides) -> Measurement:
    from datetime import datetime, timezone

    data = {
        "user_id": 1,
        "timestamp": datetime(2026, 1, 10, 8, 30, tzinfo=timezone.utc),
        "systolic": 130,
        "diastolic": 85,
    }
    data.update(overrides)
    return Measurement(**data)


class TestBuildReportPdf:
    def test_empty_measurements(self) -> None:
        payload = doctor_service.build_report_pdf("Пациент 111111", [])
        assert payload.startswith(b"%PDF")
        assert len(payload) > 500

    def test_with_measurements(self) -> None:
        measurements = [
            _measurement(),
            _measurement(pulse=72, medication_taken=True, feeling="normal"),
            _measurement(pulse=None, medication_taken=None, feeling=None),
        ]
        payload = doctor_service.build_report_pdf(
            "Пациент 111111", measurements, tz_name="Europe/Minsk"
        )
        assert payload.startswith(b"%PDF")
        assert len(payload) > 500

    def test_cyrillic_patient_label(self) -> None:
        payload = doctor_service.build_report_pdf("Пациент 111111", [], tz_name="UTC")
        assert payload.startswith(b"%PDF")

    def test_custom_generated_at(self) -> None:
        from datetime import datetime, timezone

        payload = doctor_service.build_report_pdf(
            "Пациент 111111",
            [],
            generated_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
        )
        assert payload.startswith(b"%PDF")

    def test_summary_text_without_pulse(self) -> None:
        text = doctor_service._summary_text([_measurement(pulse=None)])
        assert "Измерений: 1" in text
        assert "Пульс" not in text

    def test_summary_text_empty(self) -> None:
        assert doctor_service._summary_text([]) == "Записей нет."

    @pytest.mark.parametrize(
        ("feeling", "expected"),
        [
            ("headache", "головная боль"),
            ("weakness", "слабость"),
            ("normal", "норма"),
            ("other", "другое"),
            (None, "—"),
            ("", "—"),
        ],
    )
    def test_feeling_in_russian(self, feeling, expected) -> None:
        """Самочувствие в PDF — как в боте, а не ключ модели (`weakness`)."""
        assert doctor_service._report_feeling(feeling) == expected


# ─────────────────────────── Конкурентность (§7.2) ───────────────────────────


@pytest_asyncio.fixture
async def file_engine(tmp_path) -> AsyncEngine:
    """Файловая SQLite: WAL и busy_timeout — как в проде (раздел 7)."""
    engine = create_engine(str(tmp_path / "doctor.sqlite3"))
    await init_db(engine)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def file_session_factory(
    file_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(file_engine)


async def count(session_factory: async_sessionmaker[AsyncSession], model) -> int:
    """Число строк таблицы."""
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


class TestDoctorRaces:
    """Гонки вставок: UNIQUE отсекает проигравшего, строка ровно одна."""

    async def test_parallel_find_or_create_doctor(
        self, file_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Два конкурентных добавления одного врача: ровно один создал запись."""

        async def add() -> bool:
            async with file_session_factory() as session:
                _, created = await doctor_service.find_or_create_doctor(
                    session, "race@example.org"
                )
                return created

        results = await asyncio.gather(add(), add())
        assert sorted(results) == [False, True]
        assert await count(file_session_factory, Doctor) == 1

    async def test_parallel_link_patient(
        self, file_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Две конкурентные связки одной пары: ровно одна строка doctor_patients."""

        async with file_session_factory() as session:
            doctor, _ = await doctor_service.find_or_create_doctor(
                session, "doc@example.org"
            )
            user = User(telegram_id=TG_ID, timezone="Europe/Minsk")
            session.add(user)
            await session.commit()
            doctor_id, user_id = doctor.id, user.id

        async def link() -> bool:
            async with file_session_factory() as session:
                _, created = await doctor_service.link_patient(
                    session, doctor_id, user_id
                )
                return created

        results = await asyncio.gather(link(), link())
        assert sorted(results) == [False, True]
        assert await count(file_session_factory, DoctorPatient) == 1

    async def test_parallel_add_note(
        self, file_session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """Заметки от двух конкурентов не теряются: обе строки сохраняются."""

        async with file_session_factory() as session:
            doctor, _ = await doctor_service.find_or_create_doctor(
                session, "doc@example.org"
            )
            user = User(telegram_id=TG_ID, timezone="Europe/Minsk")
            session.add(user)
            await session.commit()
            doctor_id, user_id = doctor.id, user.id

        async def note(text: str) -> None:
            async with file_session_factory() as session:
                await doctor_service.add_note(session, doctor_id, user_id, text)

        await asyncio.gather(note("Заметка 1"), note("Заметка 2"))
        assert await count(file_session_factory, DoctorNote) == 2


class TestLinkPatientRaceRecovery:
    """Восстановление после проигранной гонки вставки (§7.2)."""

    async def test_integrity_error_refetches_existing(
        self, session: AsyncSession, user: User, mocker
    ) -> None:
        """UNIQUE на паре ловится на flush: проигравший получает строку конкурента.

        `link_for_pair` подменяется так, будто конкурент успел вставить строку
        между SELECT и INSERT: первый вызов не видит связи, INSERT падает с
        `IntegrityError`, повторный SELECT возвращает существующую строку.
        """
        doctor, _ = await doctor_service.find_or_create_doctor(session, "doc@example.org")
        existing, _ = await doctor_service.link_patient(session, doctor.id, user.id)
        existing_id = existing.id  # до rollback: атрибуты после истекают
        mocker.patch.object(
            doctor_service,
            "link_for_pair",
            new_callable=mocker.AsyncMock,
            side_effect=[None, existing],
        )
        link, created = await doctor_service.link_patient(session, doctor.id, user.id)
        assert created is False
        assert link is existing
        assert existing_id is not None
