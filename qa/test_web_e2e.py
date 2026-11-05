"""E2E-сценарии Web UI (SPEC.md, §22, пп. 4–5).

4. **Web UI:** вход в личный кабинет по magic link из бота, график давления,
   экспорт CSV.
5. **Кабинет врача:** добавление врача, «Отправить отчет», вход по ссылке,
   видимость данных пациента и заметка врача.

Ссылки берутся из реальных сообщений бота, страницы открываются Playwright;
скриншоты каждого ключевого действия — в `qa/reports/screenshots/` (CLAUDE.md).
"""
from __future__ import annotations

import re
import time

from bot import texts

#: Уникальный текст заметки врача (виден и врачу, и пациенту, §21).
NOTE_TEXT = "QA-заметка: продолжайте измерять давление утром и вечером"


def _extract_url(message) -> str:
    """Ссылка из сообщения бота (единственный URL в тексте)."""
    match = re.search(r"https?://\S+", message.text or "")
    assert match is not None, f"в сообщении бота нет ссылки: {message.text!r}"
    return match.group(0)


class TestWebE2E:
    """Сценарии §22 для Web UI и кабинета врача."""

    def test_cabinet_magic_link_and_csv(
        self, dialog, page, db, tg_me, cabinet_url, ensure_onboarded, shot
    ) -> None:
        """§22, п.4: логин по magic link, график, экспорт CSV."""
        ensure_onboarded()
        if db.measurement_count(tg_me.id) == 0:
            db.add_measurement(tg_me.id, 130, 85, 70)  # данные для графика и CSV

        # Ссылка для входа — из бота (§16.2: регистрация ЛК или повторный вход).
        message = dialog.send("/cabinet")
        if texts.CABINET_EMAIL_PROMPT in (message.text or ""):
            dialog.send(f"qa-{int(time.time())}@example.org")
            message = dialog.wait_screen(texts.CABINET_ACCOUNT_CREATED.format(url=""))
        else:
            assert texts.CABINET_LINK_SENT.format(url="") in (message.text or "")
        cabinet_link = _extract_url(message)
        assert cabinet_link.startswith(f"{cabinet_url.rstrip('/')}/login?token=")

        page.goto(cabinet_link)
        shot("cabinet_dashboard")
        content = page.content()
        assert "Личная карточка" in content, "дашборд не открылся по magic link"
        assert "История записей" in content
        assert "Сводка за период" in content
        assert page.locator("#bp-chart").count() == 1, "график давления не отрисован"

        # Экспорт CSV из кабинета (§22, п.4).
        with page.expect_download(timeout=15_000) as download_info:
            page.click("a[href*='/export/measurements.csv']")
        with open(download_info.value.path(), encoding="utf-8-sig") as csv_file:
            csv_text = csv_file.read()
        assert "130" in csv_text, "в CSV нет сохранённого измерения"
        shot("cabinet_csv_export")

    def test_doctor_portal(self, dialog, page, db, tg_me, ensure_onboarded, shot) -> None:
        """§22, п.5: отправка отчета, вход по ссылке, проверка видимости данных."""
        ensure_onboarded()
        if db.measurement_count(tg_me.id) == 0:
            db.add_measurement(tg_me.id, 130, 85, 70)
        db.reset_doctor(tg_me.id)  # чистый сценарий добавления врача

        # Добавление врача: FSM e-mail → ссылка-приглашение (§21, пп. 2–3).
        dialog.send("/start")
        dialog.wait_screen(texts.MAIN_MENU)
        dialog.press("menu:settings")
        dialog.press("set:doctor", contains=texts.DOCTOR_NO_LINK)
        dialog.send(f"qa-doctor-{int(time.time())}@example.org")
        dialog.wait_screen(texts.DOCTOR_INVITE_CREATED.format(name=tg_me.first_name, url=""))

        # «Отправить отчет» → ссылка для врача (§21, п. 5 — текст дословно).
        dialog.send("/start")
        dialog.wait_screen(texts.MAIN_MENU)
        dialog.press("menu:report", contains=texts.DOCTOR_REPORT_LINK.format(url=""))
        report_link = _extract_url(dialog.last())

        # Вход по ссылке: активация доступа и карточка пациента (§21).
        page.goto(report_link)
        shot("doctor_patient_card")
        content = page.content()
        assert "Кабинет врача" in content, "кабинет врача не открылся по ссылке"
        assert f"Пациент {tg_me.id}" in content, "пациент не виден врачу"
        assert "130" in content, "измерения пациента не видны врачу"

        # Заметка врача сохраняется и видна на странице (§21, §22, п. 5).
        page.fill("textarea[name='text']", NOTE_TEXT)
        page.click("form[action$='/note'] button[type='submit']")
        page.wait_for_selector(f"text={NOTE_TEXT}", timeout=15_000)
        shot("doctor_note_saved")
