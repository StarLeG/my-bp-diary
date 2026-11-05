"""Тесты хендлеров «О боте / Поддержать» и команд /support, /cabinet (§6.8–6.9)."""
from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot import keyboards, texts
from bot.config import Settings
from bot.handlers import support
from bot.models import User as DbUser, WebUser

TG_ID = 111_111
DONATE_URL = "https://example.org/donate"


def make_message(text: str | None = None, user_id: int = TG_ID) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        text=text,
    )


def make_callback(data: str, message: Message, user_id: int = TG_ID) -> CallbackQuery:
    return CallbackQuery(
        id="q1",
        from_user=User(id=user_id, is_bot=False, first_name="Тест"),
        chat_instance="ci",
        data=data,
        message=message,
    )


def make_state(user_id: int = TG_ID) -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
    return FSMContext(storage=storage, key=key)


async def call(handler, **kwargs) -> None:
    """Вызвать хендлер как aiogram DI: только параметры из его сигнатуры."""
    wanted = inspect.signature(handler).parameters
    await handler(**{name: value for name, value in kwargs.items() if name in wanted})


@pytest.fixture
def edit(mocker):
    return mocker.patch.object(Message, "edit_text", new_callable=mocker.AsyncMock)


@pytest.fixture
def answer(mocker):
    return mocker.patch.object(Message, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def cb_answer(mocker):
    return mocker.patch.object(CallbackQuery, "answer", new_callable=mocker.AsyncMock)


@pytest.fixture
def state() -> FSMContext:
    return make_state()


@pytest.fixture
def with_settings(mocker):
    """Подменить get_settings в support: настройки по умолчанию — всё скрыто."""

    def apply(**env) -> Settings:
        settings = Settings(bot_token="123:TEST", **env)
        mocker.patch.object(support, "get_settings", return_value=settings)
        return settings

    return apply


class TestSupportCommand:
    async def test_opens_support_screen(self, answer, state, with_settings) -> None:
        with_settings(support_url=DONATE_URL)
        await state.set_state("Draft:input")
        await call(support.cmd_support, message=make_message(), state=state)
        assert answer.call_args.args[0] == texts.SUPPORT_TEXT
        assert answer.call_args.kwargs["reply_markup"] == keyboards.support_kb(DONATE_URL)
        assert await state.get_state() is None

    async def test_without_url_shows_about(self, answer, state, with_settings) -> None:
        with_settings()
        await call(support.cmd_support, message=make_message(), state=state)
        assert answer.call_args.args[0] == texts.ABOUT_TEXT
        assert answer.call_args.kwargs["reply_markup"] == keyboards.about_kb(
            support_visible=False, author_visible=False
        )

    async def test_placeholder_url_shows_about(self, answer, state, with_settings) -> None:
        with_settings(support_url="https://example.com")
        await call(support.cmd_support, message=make_message(), state=state)
        assert answer.call_args.args[0] == texts.ABOUT_TEXT

    async def test_author_visible_in_about(self, answer, state, with_settings) -> None:
        with_settings(author_contact="@author")
        await call(support.cmd_support, message=make_message(), state=state)
        assert answer.call_args.kwargs["reply_markup"] == keyboards.about_kb(
            support_visible=False, author_visible=True
        )


class TestAboutScreen:
    async def test_menu_about(self, edit, cb_answer, with_settings) -> None:
        with_settings()
        await call(support.cb_about, callback=make_callback("menu:about", make_message()))
        assert edit.call_args.args[0] == texts.ABOUT_TEXT
        assert edit.call_args.kwargs["reply_markup"] == keyboards.about_kb(
            support_visible=False, author_visible=False
        )

    async def test_menu_about_respects_visibility(self, edit, cb_answer, with_settings) -> None:
        with_settings(support_url=DONATE_URL, author_contact="@author")
        await call(support.cb_about, callback=make_callback("menu:about", make_message()))
        assert edit.call_args.kwargs["reply_markup"] == keyboards.about_kb(
            support_visible=True, author_visible=True
        )

    async def test_support_button_shows_page(self, edit, cb_answer, with_settings) -> None:
        with_settings(support_url=DONATE_URL)
        await call(
            support.cb_about_support,
            callback=make_callback("about:support", make_message()),
        )
        assert edit.call_args.args[0] == texts.SUPPORT_TEXT
        assert edit.call_args.kwargs["reply_markup"] == keyboards.support_kb(DONATE_URL)

    async def test_hidden_support_falls_back_to_about(
        self, edit, cb_answer, with_settings
    ) -> None:
        with_settings()
        await call(
            support.cb_about_support,
            callback=make_callback("about:support", make_message()),
        )
        assert edit.call_args.args[0] == texts.ABOUT_TEXT

    async def test_author_shows_contact(self, edit, cb_answer, with_settings) -> None:
        with_settings(author_contact="@author")
        await call(
            support.cb_about_author,
            callback=make_callback("about:author", make_message()),
        )
        assert edit.call_args.args[0] == "@author"
        assert edit.call_args.kwargs["reply_markup"] == keyboards.about_kb(
            support_visible=False, author_visible=True
        )

    async def test_hidden_author_falls_back_to_about(
        self, edit, cb_answer, with_settings
    ) -> None:
        with_settings()
        await call(
            support.cb_about_author,
            callback=make_callback("about:author", make_message()),
        )
        assert edit.call_args.args[0] == texts.ABOUT_TEXT


async def make_web_user(
    session: AsyncSession, user: DbUser, email: str = "user@example.com"
) -> WebUser:
    """Аккаунт кабинета для пользователя (хеш Argon2id как в auth.py)."""
    web_user = WebUser(
        user_id=user.id, email=email, password_hash="$argon2id$v=19$m=65536,t=3,p=4$test"
    )
    session.add(web_user)
    await session.commit()
    return web_user


class TestCabinetCommand:
    async def test_disabled_without_secret(
        self, answer, state, with_settings, session
    ) -> None:
        with_settings(cabinet_url="https://cabinet.example.org")
        await call(support.cmd_cabinet, message=make_message(), state=state, session=session)
        assert answer.call_args.args[0] == texts.CABINET_DISABLED

    async def test_disabled_without_url(
        self, answer, state, with_settings, session
    ) -> None:
        with_settings(web_secret_key="secret")
        await call(support.cmd_cabinet, message=make_message(), state=state, session=session)
        assert answer.call_args.args[0] == texts.CABINET_DISABLED

    async def test_no_account_starts_registration(
        self, answer, state, with_settings, session, user
    ) -> None:
        """Без записи в web_users — FSM запроса e-mail (§16.2, п.1)."""
        with_settings(web_secret_key="secret", cabinet_url="https://cabinet.example.org")
        await call(support.cmd_cabinet, message=make_message(), state=state, session=session)
        assert answer.call_args.args[0] == texts.CABINET_EMAIL_PROMPT
        assert answer.call_args.kwargs["reply_markup"] == keyboards.input_cancel_kb(
            "cabinet:cancel"
        )
        assert await state.get_state() == support.CabinetRegistration.email.state

    async def test_sends_signed_link(
        self, answer, state, with_settings, session, user
    ) -> None:
        with_settings(web_secret_key="secret", cabinet_url="https://cabinet.example.org")
        await make_web_user(session, user)
        await call(support.cmd_cabinet, message=make_message(), state=state, session=session)
        text = answer.call_args.args[0]
        assert text.startswith("Ссылка для входа в личный кабинет")
        url = text.rsplit("\n", 1)[1]
        assert url.startswith("https://cabinet.example.org/login?token=")
        token = url.partition("token=")[2]
        payload = URLSafeTimedSerializer(
            "secret", salt=support.CABINET_SALT
        ).loads(token, max_age=support.CABINET_TOKEN_MAX_AGE)
        assert payload["uid"] == TG_ID
        assert answer.call_args.kwargs["reply_markup"] == keyboards.back_to_menu()

    async def test_resets_fsm(self, answer, state, with_settings, session) -> None:
        with_settings()
        state = make_state()
        await state.set_state("Draft:input")
        await call(support.cmd_cabinet, message=make_message(), state=state, session=session)
        assert await state.get_state() is None


class TestCabinetButton:
    """Кнопка «Личный кабинет» в главном меню (§6.2, §16.2)."""

    async def test_menu_cabinet_edits_with_signed_link(
        self, edit, cb_answer, with_settings, session, user
    ) -> None:
        with_settings(web_secret_key="secret", cabinet_url="https://cabinet.example.org")
        await make_web_user(session, user)
        await call(
            support.cb_cabinet,
            callback=make_callback("menu:cabinet", make_message()),
            state=make_state(),
            session=session,
        )
        text = edit.call_args.args[0]
        assert text.startswith("Ссылка для входа в личный кабинет")
        url = text.rsplit("\n", 1)[1]
        assert url.startswith("https://cabinet.example.org/login?token=")
        token = url.partition("token=")[2]
        payload = URLSafeTimedSerializer(
            "secret", salt=support.CABINET_SALT
        ).loads(token, max_age=support.CABINET_TOKEN_MAX_AGE)
        assert payload["uid"] == TG_ID
        assert edit.call_args.kwargs["reply_markup"] == keyboards.back_to_menu()
        cb_answer.assert_awaited()

    async def test_no_account_prompts_email(
        self, edit, cb_answer, with_settings, session, user
    ) -> None:
        """Без аккаунта кнопка тоже запускает FSM (§6.2: «запускает FSM-диалог»)."""
        with_settings(web_secret_key="secret", cabinet_url="https://cabinet.example.org")
        state = make_state()
        await call(
            support.cb_cabinet,
            callback=make_callback("menu:cabinet", make_message()),
            state=state,
            session=session,
        )
        assert edit.call_args.args[0] == texts.CABINET_EMAIL_PROMPT
        assert edit.call_args.kwargs["reply_markup"] == keyboards.input_cancel_kb(
            "cabinet:cancel"
        )
        assert await state.get_state() == support.CabinetRegistration.email.state

    async def test_disabled_without_secret(
        self, edit, cb_answer, with_settings, session
    ) -> None:
        with_settings(cabinet_url="https://cabinet.example.org")
        await call(
            support.cb_cabinet,
            callback=make_callback("menu:cabinet", make_message()),
            state=make_state(),
            session=session,
        )
        assert edit.call_args.args[0] == texts.CABINET_DISABLED


class TestCabinetRegistration:
    """FSM `CabinetRegistration.email`: формат, занятость, создание (§16.2)."""

    async def _in_state(self) -> FSMContext:
        state = make_state()
        await state.set_state(support.CabinetRegistration.email)
        return state

    async def test_bad_format_keeps_state(
        self, answer, with_settings, session, user
    ) -> None:
        with_settings(web_secret_key="secret", cabinet_url="https://cabinet.example.org")
        state = await self._in_state()
        await call(
            support.msg_cabinet_email,
            message=make_message("не email"),
            state=state,
            session=session,
        )
        assert answer.call_args.args[0] == texts.CABINET_EMAIL_BAD
        assert await state.get_state() == support.CabinetRegistration.email.state

    async def test_empty_text_rejected(
        self, answer, with_settings, session, user
    ) -> None:
        with_settings(web_secret_key="secret", cabinet_url="https://cabinet.example.org")
        state = await self._in_state()
        await call(
            support.msg_cabinet_email,
            message=make_message("   "),
            state=state,
            session=session,
        )
        assert answer.call_args.args[0] == texts.CABINET_EMAIL_BAD

    async def test_valid_email_creates_account_and_link(
        self, answer, with_settings, session
    ) -> None:
        """Успех: запись web_users (Argon2id-хеш), ссылка, FSM сброшен (§16.2)."""
        with_settings(web_secret_key="secret", cabinet_url="https://cabinet.example.org")
        state = await self._in_state()
        await call(
            support.msg_cabinet_email,
            message=make_message("User@Example.COM"),
            state=state,
            session=session,
        )
        text = answer.call_args.args[0]
        assert text.startswith("Аккаунт создан. Ваша ссылка для входа:")
        url = text.rsplit("\n", 1)[1]
        assert url.startswith("https://cabinet.example.org/login?token=")
        assert answer.call_args.kwargs["reply_markup"] == keyboards.back_to_menu()
        assert await state.get_state() is None
        from sqlalchemy import select

        web_user = (
            await session.execute(select(WebUser))
        ).scalar_one()
        # e-mail нормализуется; пароль — только хеш Argon2id.
        assert web_user.email == "user@example.com"
        assert web_user.password_hash.startswith("$argon2id$")

    async def test_taken_email_asks_another(
        self, answer, with_settings, session, user
    ) -> None:
        """Занятый e-mail (UNIQUE) — ошибка и просьба ввести другой (§16.2, п.3)."""
        with_settings(web_secret_key="secret", cabinet_url="https://cabinet.example.org")
        other = DbUser(telegram_id=222_222)
        session.add(other)
        await session.flush()
        await make_web_user(session, other, email="busy@example.com")
        state = await self._in_state()
        await call(
            support.msg_cabinet_email,
            message=make_message("busy@example.com"),
            state=state,
            session=session,
        )
        assert answer.call_args.args[0] == texts.CABINET_EMAIL_TAKEN
        assert await state.get_state() == support.CabinetRegistration.email.state

    async def test_integrity_race_treated_as_taken(
        self, answer, with_settings, session, user, mocker
    ) -> None:
        """Гонка двух вводов: проигравший коммит — «e-mail занят», не падение (§7.2)."""
        with_settings(web_secret_key="secret", cabinet_url="https://cabinet.example.org")
        uid = user.id  # до rollback: откат истекает атрибуты ORM-объекта
        state = await self._in_state()
        mocker.patch.object(
            session,
            "flush",
            new_callable=mocker.AsyncMock,
            side_effect=IntegrityError("INSERT", {}, Exception("UNIQUE")),
        )
        await call(
            support.msg_cabinet_email,
            message=make_message("race@example.com"),
            state=state,
            session=session,
        )
        assert answer.call_args.args[0] == texts.CABINET_EMAIL_TAKEN
        assert await state.get_state() == support.CabinetRegistration.email.state
        # К объекту после rollback не прикасаемся (атрибуты истекли) — только по id.
        assert (await session.get(DbUser, uid)) is not None

    async def test_cancel_returns_to_menu(self, edit, cb_answer, with_settings) -> None:
        state = await self._in_state()
        await call(
            support.cb_cabinet_cancel,
            callback=make_callback("cabinet:cancel", make_message()),
            state=state,
        )
        assert await state.get_state() is None
        assert edit.call_args.args[0] == texts.MAIN_MENU
        assert edit.call_args.kwargs["reply_markup"] == keyboards.main_menu()
        cb_answer.assert_awaited_once_with(texts.CABINET_REG_CANCELLED)


class TestMakeCabinetLink:
    def test_link_format(self) -> None:
        settings = Settings(
            bot_token="123:TEST",
            web_secret_key="secret",
            cabinet_url="https://cabinet.example.org/",
        )
        link = support.make_cabinet_link(settings, TG_ID)
        assert link.startswith("https://cabinet.example.org/login?token=")

    def test_token_roundtrip(self) -> None:
        settings = Settings(bot_token="123:TEST", web_secret_key="secret", cabinet_url="x")
        link = support.make_cabinet_link(settings, TG_ID)
        token = link.partition("token=")[2]
        serializer = URLSafeTimedSerializer("secret", salt=support.CABINET_SALT)
        payload = serializer.loads(token, max_age=support.CABINET_TOKEN_MAX_AGE)
        # Формат payload — как в web/cabinet/auth.py::MagicLink (uid + jti).
        assert payload["uid"] == TG_ID
        assert isinstance(payload["jti"], str) and payload["jti"]

    def test_foreign_secret_rejected(self) -> None:
        settings = Settings(bot_token="123:TEST", web_secret_key="secret", cabinet_url="x")
        token = support.make_cabinet_link(settings, TG_ID).partition("token=")[2]
        with pytest.raises(BadSignature):
            URLSafeTimedSerializer("other", salt=support.CABINET_SALT).loads(
                token, max_age=support.CABINET_TOKEN_MAX_AGE
            )
