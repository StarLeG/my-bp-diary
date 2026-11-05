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

from bot import keyboards, texts
from bot.config import Settings
from bot.handlers import support

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


class TestCabinetCommand:
    async def test_disabled_without_secret(self, answer, state, with_settings) -> None:
        with_settings(cabinet_url="https://cabinet.example.org")
        await call(support.cmd_cabinet, message=make_message(), state=state)
        assert answer.call_args.args[0] == texts.CABINET_DISABLED

    async def test_disabled_without_url(self, answer, state, with_settings) -> None:
        with_settings(web_secret_key="secret")
        await call(support.cmd_cabinet, message=make_message(), state=state)
        assert answer.call_args.args[0] == texts.CABINET_DISABLED

    async def test_sends_signed_link(self, answer, state, with_settings) -> None:
        with_settings(web_secret_key="secret", cabinet_url="https://cabinet.example.org")
        await call(support.cmd_cabinet, message=make_message(), state=state)
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

    async def test_resets_fsm(self, answer, state, with_settings) -> None:
        with_settings()
        state = make_state()
        await state.set_state("Draft:input")
        await call(support.cmd_cabinet, message=make_message(), state=state)
        assert await state.get_state() is None


class TestCabinetButton:
    """Кнопка «Личный кабинет» в главном меню (§16.2)."""

    async def test_menu_cabinet_edits_with_signed_link(
        self, edit, cb_answer, with_settings
    ) -> None:
        with_settings(web_secret_key="secret", cabinet_url="https://cabinet.example.org")
        await call(
            support.cb_cabinet,
            callback=make_callback("menu:cabinet", make_message()),
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

    async def test_disabled_without_secret(self, edit, cb_answer, with_settings) -> None:
        with_settings(cabinet_url="https://cabinet.example.org")
        await call(
            support.cb_cabinet,
            callback=make_callback("menu:cabinet", make_message()),
        )
        assert edit.call_args.args[0] == texts.CABINET_DISABLED


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
