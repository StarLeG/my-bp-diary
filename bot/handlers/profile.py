"""«Мои данные»: ФИО, дата рождения и пол для врача (SPEC.md, §6.12).

- «Настройки» → «👤 Мои данные»: П1 (не заполнены) / П2 (заполнены),
  «Изменить», «Удалить данные».
- Ввод — FSM `PatientProfile`: фамилия → имя → отчество («Нет отчества») →
  дата рождения → пол кнопками. Черновик — в FSM (`pf_*`), в БД данные
  попадают целиком на последнем шаге. Младше 16 лет — не сохраняются (П5).
- Перед приглашением врача (`pf_then_invite`) после сохранения — выбор
  способа приглашения.
- П7 — разовое предложение заполнить данные пациенту с врачом.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, schemas, texts
from bot.models import User
from bot.services import measurements as measurements_service
from bot.services import profile as profile_service

router = Router(name="profile")


class PatientProfile(StatesGroup):
    """FSM ввода данных пациента (SPEC.md, §8)."""

    last_name = State()
    first_name = State()
    middle_name = State()
    birth_date = State()


def profile_screen(user: User) -> tuple[str, InlineKeyboardMarkup]:
    """П1 / П2 — экран «Мои данные» (шапка `<b>`, показ — с parse_mode="HTML")."""
    if not profile_service.has_profile(user):
        return texts.PROFILE_EMPTY, keyboards.profile_kb(filled=False)
    return (
        texts.PROFILE_FILLED.format(
            name=texts.esc(profile_service.full_name(user)),
            birth=f"{user.birth_date:%d.%m.%Y}",
            age=profile_service.age_text(user),
            sex=profile_service.SEX_LABELS[user.sex][0],  # type: ignore[index]
        ),
        keyboards.profile_kb(filled=True),
    )


def prompt_screen() -> tuple[str, InlineKeyboardMarkup]:
    """П7 — предложение заполнить данные."""
    return texts.PROFILE_PROMPT, keyboards.profile_prompt_kb()


def _with_current(prompt: str, value: Optional[str]) -> str:
    """Подсказка шага; при изменении — «Сейчас: …»."""
    return f"{prompt}\n{texts.PROFILE_CURRENT.format(value=value)}" if value else prompt


async def start_input(
    state: FSMContext, user: User, *, then_invite: bool = False
) -> tuple[str, InlineKeyboardMarkup]:
    """Начать ввод: первый шаг (фамилия). `then_invite` — после — приглашение врача."""
    await state.clear()
    await state.set_state(PatientProfile.last_name)
    await state.update_data(pf_then_invite=then_invite)
    prompt = _with_current(texts.PROFILE_ASK_LAST, user.last_name)
    if then_invite:
        prompt = f"{texts.PROFILE_BEFORE_INVITE}\n\n{prompt}"
    return prompt, keyboards.input_cancel_kb("prof:show")


# ─────────────────────────── Экран ───────────────────────────


@router.callback_query(F.data.in_({"set:profile", "prof:show"}))
async def cb_profile(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«👤 Мои данные» / «Отмена» на шаге ввода: экран П1 / П2."""
    await state.clear()
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    text, markup = profile_screen(user)
    await callback.message.edit_text(  # type: ignore[union-attr]
        text, reply_markup=markup, parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "prof:edit")
async def cb_edit(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«Заполнить» / «Изменить»: шаг фамилии."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    text, markup = await start_input(state, user)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.callback_query(F.data == "prof:later")
async def cb_later(callback: CallbackQuery, session: AsyncSession) -> None:
    """«Позже» (П7): главное меню; больше не предлагаем."""
    from bot.services import doctor as doctor_service

    is_doctor = await doctor_service.doctor_by_telegram(session, callback.from_user.id) is not None
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.MAIN_MENU, reply_markup=keyboards.main_menu(is_doctor=is_doctor)
    )
    await callback.answer()


# ─────────────────────────── Шаги ввода ───────────────────────────


async def _name_step(
    message: Message,
    state: FSMContext,
    key: str,
    next_state: State,
    next_prompt: str,
    next_markup: InlineKeyboardMarkup,
    current: Optional[str],
) -> None:
    """Шаг ФИО: проверка, черновик, следующий шаг или ошибка П4."""
    try:
        value = schemas.validate_person_name(message.text or "")
    except ValueError:
        await message.answer(texts.PROFILE_NAME_BAD, reply_markup=keyboards.input_cancel_kb("prof:show"))
        return
    await state.update_data(**{key: value})
    await state.set_state(next_state)
    await message.answer(_with_current(next_prompt, current), reply_markup=next_markup)


@router.message(PatientProfile.last_name)
async def msg_last_name(message: Message, state: FSMContext, session: AsyncSession) -> None:
    user = await measurements_service.get_or_create_user(session, message.from_user.id)  # type: ignore[union-attr]
    await _name_step(
        message, state, "pf_last", PatientProfile.first_name, texts.PROFILE_ASK_FIRST,
        keyboards.input_cancel_kb("prof:show"), user.first_name,
    )


@router.message(PatientProfile.first_name)
async def msg_first_name(message: Message, state: FSMContext, session: AsyncSession) -> None:
    user = await measurements_service.get_or_create_user(session, message.from_user.id)  # type: ignore[union-attr]
    await _name_step(
        message, state, "pf_first", PatientProfile.middle_name, texts.PROFILE_ASK_MIDDLE,
        keyboards.profile_middle_kb(), user.middle_name,
    )


async def _ask_birth(state: FSMContext, user: User) -> tuple[str, InlineKeyboardMarkup]:
    await state.set_state(PatientProfile.birth_date)
    current = f"{user.birth_date:%d.%m.%Y}" if user.birth_date else None
    return _with_current(texts.PROFILE_ASK_BIRTH, current), keyboards.input_cancel_kb("prof:show")


@router.message(PatientProfile.middle_name)
async def msg_middle_name(message: Message, state: FSMContext, session: AsyncSession) -> None:
    try:
        value = schemas.validate_person_name(message.text or "")
    except ValueError:
        await message.answer(texts.PROFILE_NAME_BAD, reply_markup=keyboards.profile_middle_kb())
        return
    await state.update_data(pf_middle=value)
    user = await measurements_service.get_or_create_user(session, message.from_user.id)  # type: ignore[union-attr]
    text, markup = await _ask_birth(state, user)
    await message.answer(text, reply_markup=markup)


@router.callback_query(PatientProfile.middle_name, F.data == "prof:nomiddle")
async def cb_no_middle(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """«Нет отчества»."""
    await state.update_data(pf_middle=None)
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    text, markup = await _ask_birth(state, user)
    await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
    await callback.answer()


@router.message(PatientProfile.birth_date)
async def msg_birth(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Дата рождения: формат и диапазон (П4), младше 16 — П5, данные не сохраняются."""
    user = await measurements_service.get_or_create_user(session, message.from_user.id)  # type: ignore[union-attr]
    try:
        birth = schemas.parse_birth_date(message.text or "", profile_service.today_for(user))
    except schemas.TooYoungError:
        await state.clear()
        await message.answer(texts.PROFILE_TOO_YOUNG, reply_markup=_back_kb())
        return
    except ValueError:
        await message.answer(texts.PROFILE_BIRTH_BAD, reply_markup=keyboards.input_cancel_kb("prof:show"))
        return
    await state.update_data(pf_birth=birth.isoformat())
    current = profile_service.SEX_LABELS[user.sex][0] if user.sex else None
    await message.answer(
        _with_current(texts.PROFILE_ASK_SEX, current), reply_markup=keyboards.profile_sex_kb()
    )


def _back_kb() -> InlineKeyboardMarkup:
    """Только «Назад» — в настройки."""
    from aiogram.types import InlineKeyboardButton
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text=texts.BTN_BACK, callback_data="set:back"))
    return builder.as_markup()


@router.callback_query(PatientProfile.birth_date, F.data.startswith("prof:sex:"))
async def cb_sex(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Пол — последний шаг: проверка целиком и сохранение (П6)."""
    data = await state.get_data()
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    try:
        personal = profile_service.validate(
            data.get("pf_last", ""),
            data.get("pf_first", ""),
            data.get("pf_middle"),
            date.fromisoformat(data.get("pf_birth", "")),
            (callback.data or "").rsplit(":", 1)[-1],
            profile_service.today_for(user),
        )
    except ValueError:  # черновик неполный (кнопка из старого сообщения)
        text, markup = await start_input(state, user)
        await callback.message.edit_text(text, reply_markup=markup)  # type: ignore[union-attr]
        await callback.answer()
        return
    then_invite = bool(data.get("pf_then_invite"))
    await state.clear()
    await profile_service.save(session, user, personal)
    if then_invite:
        from bot.handlers.doctor import _doctor_entry

        text, markup = await _doctor_entry(session, state, callback.from_user.id)
        html = False  # «Мой врач» — без канонической шапки
    else:
        text, markup = profile_screen(user)
        html = True  # шапка <b> (§11)
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"{texts.PROFILE_SAVED}\n\n{text}", reply_markup=markup,
        parse_mode="HTML" if html else None,
    )
    await callback.answer()


# ─────────────────────────── Удаление ───────────────────────────


@router.callback_query(F.data == "prof:del")
async def cb_delete(callback: CallbackQuery) -> None:
    await callback.message.edit_text(  # type: ignore[union-attr]
        texts.PROFILE_DELETE_CONFIRM, reply_markup=keyboards.profile_delete_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "prof:del:ok")
async def cb_delete_ok(callback: CallbackQuery, session: AsyncSession) -> None:
    """«Да, удалить»: данные стёрты, врач снова видит имя из Telegram."""
    user = await measurements_service.get_or_create_user(session, callback.from_user.id)
    await profile_service.clear(session, user)
    text, markup = profile_screen(user)
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"{texts.PROFILE_DELETED}\n\n{text}", reply_markup=markup, parse_mode="HTML"
    )
    await callback.answer()
