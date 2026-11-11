"""Цветные зоны АД для графиков сводки (SPEC.md, раздел 16.2).

Пороги — константы приложения (ESC/ESH + ВОЗ, информационная подсветка без
диагностики) и не связаны с персональной «целью» пользователя
(`users.target_systolic`/`target_diastolic`). Панель «Пульс» зон не имеет —
ключа `pulse` в словаре нет.

Каждая полоса описана верхней границей `to` (входит в полосу; `None` —
открытая верхняя полоса): рисуется от предыдущей границы (или низа графика)
до `to` (или верха графика). `fill` — заливка полосы, `line` — цвет
горизонтальной линии порога (у верхней открытой полосы линии нет).
"""
from __future__ import annotations

#: Зоны по показателю: ключи `systolic` (САД) и `diastolic` (ДАД), мм рт. ст.
CHART_ZONES: dict[str, list[dict]] = {
    "systolic": [
        {
            "to": 130,
            "label": "< 130",
            "fill": "rgba(30,125,79,0.12)",
            "line": "rgba(30,125,79,0.55)",
        },
        {
            "to": 140,
            "label": "130–139",
            "fill": "rgba(217,164,0,0.14)",
            "line": "rgba(217,164,0,0.55)",
        },
        {
            "to": None,
            "label": "≥ 140",
            "fill": "rgba(192,57,43,0.12)",
            "line": None,
        },
    ],
    "diastolic": [
        {
            "to": 85,
            "label": "< 85",
            "fill": "rgba(30,125,79,0.12)",
            "line": "rgba(30,125,79,0.55)",
        },
        {
            "to": 90,
            "label": "85–89",
            "fill": "rgba(217,164,0,0.14)",
            "line": "rgba(217,164,0,0.55)",
        },
        {
            "to": None,
            "label": "≥ 90",
            "fill": "rgba(192,57,43,0.12)",
            "line": None,
        },
    ],
}


def band_index(value: int, bands: list[dict]) -> int:
    """Номер полосы значения: 0 — зелёная, 1 — жёлтая, 2 — красная.

    Значение попадает в первую полосу, чья граница `to` больше него
    (130 — уже жёлтая, 140 — уже красная, как в подписях «< 130», «130–139»).
    """
    for index, band in enumerate(bands):
        if band["to"] is None or value < band["to"]:
            return index
    raise ValueError("у последней полосы нет открытой верхней границы")


#: CSS-класс строки «Истории записей» по номеру худшей полосы (§16.2):
#: зелёная (0) не подсвечивается.
ROW_ZONE_CLASSES = {1: "zone-row-elevated", 2: "zone-row-high"}


def row_zone(systolic: int, diastolic: int) -> dict | None:
    """Подсветка строки «Истории записей» по худшей из зон САД и ДАД (§16.2).

    None — обе зоны зелёные (строка без подсветки). Иначе — CSS-класс,
    заливка худшей полосы (из `CHART_ZONES`, как у графиков) и `title`:
    зоны показателей вне нормы («САД: ≥ 140, ДАД: 85–89»).
    """
    levels = {
        "САД": band_index(systolic, CHART_ZONES["systolic"]),
        "ДАД": band_index(diastolic, CHART_ZONES["diastolic"]),
    }
    worst = max(levels.values())
    if worst == 0:
        return None
    keys = {"САД": "systolic", "ДАД": "diastolic"}
    title = ", ".join(
        f"{name}: {CHART_ZONES[keys[name]][level]['label']}"
        for name, level in levels.items()
        if level > 0
    )
    return {
        "css_class": ROW_ZONE_CLASSES[worst],
        "fill": CHART_ZONES["systolic"][worst]["fill"],
        "title": title,
    }


#: Точка зоны дня в заголовке «Истории записей» (§16.2, 0.5.9): по худшей
#: из зон округлённых средних САД/ДАД — как подсветка строки «Средних по дням».
DAY_ZONE_DOTS = ("🟢", "🟡", "🔴")


def day_zone(systolic: int, diastolic: int) -> dict:
    """Точка и пояснение зоны дня по округлённым средним САД и ДАД (§16.2).

    Уровень — худший из зон показателей (`band_index`): 0 — норма, 1 —
    повышенное, 2 — высокое. `title` — те же подписи зон, что у строки
    «Истории записей» («САД: 130–139, ДАД: < 85»).
    """
    levels = {
        "САД": band_index(systolic, CHART_ZONES["systolic"]),
        "ДАД": band_index(diastolic, CHART_ZONES["diastolic"]),
    }
    keys = {"САД": "systolic", "ДАД": "diastolic"}
    title = ", ".join(
        f"{name}: {CHART_ZONES[keys[name]][level]['label']}"
        for name, level in levels.items()
    )
    return {"dot": DAY_ZONE_DOTS[max(levels.values())], "title": title}


def table_legend() -> list[dict]:
    """Легенда над таблицей истории: две плашки — жёлтая и красная (§16.2)."""
    return [
        {
            "fill": CHART_ZONES["systolic"][level]["fill"],
            "label": (
                f"САД {CHART_ZONES['systolic'][level]['label']}"
                f" · ДАД {CHART_ZONES['diastolic'][level]['label']}"
            ),
        }
        for level in (1, 2)
    ]
