"""Тесты сервиса согласий (SPEC.md, §4.15, §6.11).

Без триггеров согласия по умолчанию (`real_consent`): каждое согласие здесь
даётся явно. Покрывается: основное согласие (идемпотентность, гонка двух
«Согласен», замена версией, отзыв в кабинете), согласие на врача (запись без
врача, привязка, закрытие), связи без согласия (Д4), согласие врача и три
условия доступа врача к пациенту.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.models import Consent, Doctor, DoctorInvite, DoctorPatient, User
from bot.services import consent
from bot.services import doctor as doctor_service

pytestmark = pytest.mark.real_consent

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)


async def make_doctor(session: AsyncSession, telegram_id: int = 900, *, agreed: bool = True) -> Doctor:
    doctor = Doctor(telegram_id=telegram_id, name="Анна Иванова", password_hash="x")
    if agreed:
        doctor.consent_version = consent.DOCTOR_CONSENT_VERSION
        doctor.consent_at = NOW
        doctor.consent_channel = "bot"
    session.add(doctor)
    await session.commit()
    return doctor


async def link(session: AsyncSession, doctor: Doctor, user: User, status: str = "active") -> DoctorPatient:
    row = DoctorPatient(doctor_id=doctor.id, user_id=user.id, status=status)
    session.add(row)
    await session.commit()
    return row


async def count(session: AsyncSession, **where) -> int:
    query = select(func.count(Consent.id))
    for key, value in where.items():
        column = getattr(Consent, key)
        query = query.where(column.is_(None) if value is None else column == value)
    return int((await session.execute(query)).scalar_one())


def test_display_date() -> None:
    assert consent.display_date("2026-11-10") == "10.11.2026"


class TestProcessing:
    async def test_none_by_default(self, session: AsyncSession, user: User) -> None:
        assert await consent.current_processing(session, user.id) is None
        assert not await consent.has_processing(session, user.id)

    async def test_give_is_idempotent(self, session: AsyncSession, user: User) -> None:
        first = await consent.give_processing(session, user.id, consent.CHANNEL_BOT, NOW)
        second = await consent.give_processing(session, user.id, consent.CHANNEL_CABINET)
        assert first.id == second.id
        assert first.version == consent.CONSENT_VERSION and first.channel == "bot"
        assert await consent.has_processing(session, user.id)
        assert await count(session, kind="processing") == 1

    async def test_unknown_user(self, session: AsyncSession) -> None:
        assert not await consent.has_processing(session, 999_999)

    async def test_old_version_replaced(self, session: AsyncSession, user: User) -> None:
        session.add(Consent(user_id=user.id, kind="processing", version="2000-01-01", channel="bot", given_at=NOW))
        await session.commit()
        assert not await consent.has_processing(session, user.id)
        fresh = await consent.give_processing(session, user.id, consent.CHANNEL_BOT)
        rows = (await session.execute(select(Consent).order_by(Consent.id))).scalars().all()
        assert [row.withdrawn_at is not None for row in rows] == [True, False]
        assert rows[1].id == fresh.id

    async def test_double_tap_creates_one_row(
        self, session_factory: async_sessionmaker[AsyncSession], user: User
    ) -> None:
        """Два «Согласен» одновременно — одна действующая запись (§7.2)."""

        async def tap() -> int:
            async with session_factory() as s:
                return (await consent.give_processing(s, user.id, consent.CHANNEL_BOT)).id

        ids = await asyncio.gather(tap(), tap())
        assert ids[0] == ids[1]
        async with session_factory() as s:
            assert await count(s, kind="processing") == 1

    async def test_unique_index_blocks_second_open_row(self, session: AsyncSession, user: User) -> None:
        await consent.give_processing(session, user.id, consent.CHANNEL_BOT)
        session.add(Consent(user_id=user.id, kind="processing", version="x", channel="bot", given_at=NOW))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_processing_rejects_doctor_id(self, session: AsyncSession, user: User) -> None:
        doctor = await make_doctor(session)
        session.add(Consent(user_id=user.id, kind="processing", doctor_id=doctor.id, version="v", channel="bot", given_at=NOW))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    async def test_request_deletion(self, session: AsyncSession, user: User) -> None:
        doctor = await make_doctor(session)
        await consent.give_processing(session, user.id, consent.CHANNEL_CABINET)
        await consent.give_doctor(session, user.id, doctor.id)
        await consent.request_deletion(session, user.id, NOW)
        await session.refresh(user)
        assert user.deletion_requested_at == NOW
        assert await count(session, withdrawn_at=None) == 0
        assert not await consent.has_processing(session, user.id)
        assert [u.id for u in await consent.users_to_delete(session)] == [user.id]

    async def test_count_with_processing(self, session: AsyncSession, user: User) -> None:
        other = User(telegram_id=222_222)
        session.add(other)
        await session.commit()
        assert await consent.count_with_processing(session) == 0
        await consent.give_processing(session, user.id, consent.CHANNEL_BOT)
        await consent.give_processing(session, other.id, consent.CHANNEL_BOT)
        await consent.request_deletion(session, other.id)
        assert await consent.count_with_processing(session) == 1


class TestDoctorConsent:
    async def test_invite_consent_reused(self, session: AsyncSession, user: User) -> None:
        assert not await consent.has_invite_consent(session, user.id)
        first = await consent.give_doctor(session, user.id, None, now=NOW)
        second = await consent.give_doctor(session, user.id, None)
        assert first.id == second.id and first.doctor_id is None
        assert await consent.has_invite_consent(session, user.id)

    async def test_double_tap_invite_consent(
        self, session_factory: async_sessionmaker[AsyncSession], user: User
    ) -> None:
        async def tap() -> int:
            async with session_factory() as s:
                return (await consent.give_doctor(s, user.id, None)).id

        ids = await asyncio.gather(tap(), tap())
        assert ids[0] == ids[1]

    async def test_attach(self, session: AsyncSession, user: User) -> None:
        doctor = await make_doctor(session)
        assert not await consent.attach_doctor(session, user.id, doctor.id)  # нет записи без врача
        await consent.give_doctor(session, user.id, None)
        assert await consent.attach_doctor(session, user.id, doctor.id)
        assert await count(session, kind="doctor", doctor_id=doctor.id) == 1
        assert not await consent.has_invite_consent(session, user.id)
        assert await consent.attach_doctor(session, user.id, doctor.id)  # уже привязано

    async def test_withdraw(self, session: AsyncSession, user: User) -> None:
        doctor = await make_doctor(session)
        await consent.give_doctor(session, user.id, None)
        await consent.give_doctor(session, user.id, doctor.id)
        await consent.withdraw_doctor(session, user.id, None, NOW)
        assert not await consent.has_invite_consent(session, user.id)
        assert await count(session, doctor_id=doctor.id, withdrawn_at=None) == 1
        await consent.withdraw_doctor(session, user.id, doctor.id)
        assert await count(session, withdrawn_at=None) == 0

    async def test_doctor_delete_cascades(self, session: AsyncSession, user: User) -> None:
        doctor = await make_doctor(session)
        await consent.give_doctor(session, user.id, None)
        await consent.give_doctor(session, user.id, doctor.id)
        await session.delete(doctor)
        await session.commit()
        assert await count(session, kind="doctor") == 1  # осталась запись без врача

    async def test_links_to_confirm(self, session: AsyncSession, user: User) -> None:
        active = await make_doctor(session, 901)
        pending = await make_doctor(session, 902)
        revoked = await make_doctor(session, 903)
        await link(session, active, user, "active")
        await link(session, pending, user, "pending")
        await link(session, revoked, user, "revoked")
        rows = await consent.links_to_confirm(session, user.id)
        assert [doc.id for _, doc in rows] == [active.id, pending.id]
        await consent.give_doctor(session, user.id, active.id)
        rows = await consent.links_to_confirm(session, user.id)
        assert [doc.id for _, doc in rows] == [pending.id]

    async def test_cancel_pending_invites(self, session: AsyncSession, user: User) -> None:
        invite = await doctor_service.create_invite(session, user.id, "Тест")
        await consent.cancel_pending_invites(session, user.id, NOW)
        assert (await session.get(DoctorInvite, invite.code, populate_existing=True)).used_at == NOW


class TestDoctorSelf:
    async def test_give_doctor_self(self, session: AsyncSession) -> None:
        doctor = await make_doctor(session, agreed=False)
        assert not consent.doctor_ok(doctor)
        await consent.give_doctor_self(session, doctor, consent.CHANNEL_CABINET, NOW)
        assert consent.doctor_ok(doctor)
        assert doctor.consent_channel == "cabinet" and doctor.consent_at == NOW
        await consent.give_doctor_self(session, doctor, consent.CHANNEL_BOT)  # уже есть — без изменений
        assert doctor.consent_channel == "cabinet"

    async def test_old_doctor_version_not_ok(self, session: AsyncSession) -> None:
        doctor = await make_doctor(session)
        doctor.consent_version = "2000-01-01"
        assert not consent.doctor_ok(doctor)


class TestAccess:
    async def _setup(self, session: AsyncSession, user: User):
        doctor = await make_doctor(session)
        await link(session, doctor, user, "active")
        await consent.give_processing(session, user.id, consent.CHANNEL_BOT)
        await consent.give_doctor(session, user.id, doctor.id)
        return doctor

    async def test_all_three_conditions(self, session: AsyncSession, user: User) -> None:
        doctor = await self._setup(session, user)
        assert await consent.doctor_can_see(session, doctor, user.id)
        assert [u.id for _, u in await consent.visible_patients(session, doctor)] == [user.id]

    async def test_without_patient_doctor_consent(self, session: AsyncSession, user: User) -> None:
        doctor = await self._setup(session, user)
        await consent.withdraw_doctor(session, user.id, doctor.id)
        assert not await consent.doctor_can_see(session, doctor, user.id)
        assert await consent.visible_patients(session, doctor) == []

    async def test_without_doctor_consent(self, session: AsyncSession, user: User) -> None:
        doctor = await self._setup(session, user)
        doctor.consent_at = None
        assert not await consent.doctor_can_see(session, doctor, user.id)
        assert await consent.visible_patients(session, doctor) == []

    async def test_without_processing_consent(self, session: AsyncSession, user: User) -> None:
        doctor = await self._setup(session, user)
        await session.execute(
            Consent.__table__.update()
            .where(Consent.kind == "processing")
            .values(withdrawn_at=NOW)
        )
        await session.commit()
        assert not await consent.doctor_can_see(session, doctor, user.id)

    async def test_not_active_link(self, session: AsyncSession, user: User) -> None:
        doctor = await self._setup(session, user)
        await doctor_service.revoke_access(session, user.id, doctor.id)
        assert not await consent.doctor_can_see(session, doctor, user.id)

    async def test_deletion_requested(self, session: AsyncSession, user: User) -> None:
        doctor = await self._setup(session, user)
        user.deletion_requested_at = NOW
        await session.commit()
        assert not await consent.doctor_can_see(session, doctor, user.id)
        assert await consent.visible_patients(session, doctor) == []
