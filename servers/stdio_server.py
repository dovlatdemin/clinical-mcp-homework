"""Задание 1. Локальный MCP-сервер клинических шкал реабилитации (транспорт stdio).

Запуск из корня homework_mcp:

    python -m servers.stdio_server

Сервер обменивается сообщениями JSON-RPC 2.0 строго через sys.stdin/sys.stdout.
Журналирование ведётся только в sys.stderr, чтобы не нарушать протокольный поток
(см. контрольный вопрос 2 практической работы).

Инструменты:
    modified_rankin_scale      — модифицированная шкала Ранкина (mRS), 0-6;
    rivermead_mobility_index   — индекс мобильности Ривмид, 15 пунктов, 0-15;
    rehab_routing_scale        — шкала реабилитационной маршрутизации (ШРМ), 0-5;
    list_calculators           — мета-инструмент: описания методик, границы, единицы.

Все параметры строго типизированы (type hints + Pydantic Field). Ошибки входных
данных поднимаются как ValueError и на стороне клиента превращаются в ToolError.
"""

import logging
import sys
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

# Логи — ТОЛЬКО в stderr. print() внутри сервера запрещён: он испортит stdout-поток JSON-RPC.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
log = logging.getLogger("rehab-scales-stdio")

mcp = FastMCP("rehab-scales")

# ---------------------------------------------------------------------------
# Справочные данные методик
# ---------------------------------------------------------------------------

MRS_GRADES = {
    0: ("Нет симптомов", "Симптомы заболевания отсутствуют."),
    1: ("Нет значимой инвалидизации",
        "Симптомы есть, но пациент выполняет все прежние обязанности и виды активности."),
    2: ("Лёгкая инвалидизация",
        "Не может выполнять все прежние виды деятельности, но обслуживает себя без посторонней помощи."),
    3: ("Умеренная инвалидизация",
        "Требуется некоторая помощь в повседневной жизни, однако ходит без посторонней поддержки."),
    4: ("Умеренно тяжёлая инвалидизация",
        "Не может ходить без посторонней помощи и не может обслуживать себя самостоятельно."),
    5: ("Тяжёлая инвалидизация",
        "Прикован к постели, нуждается в постоянном сестринском уходе и внимании."),
    6: ("Смерть", "Летальный исход."),
}
MRS_SOURCE = ("van Swieten J.C. et al. Interobserver agreement for the assessment "
              "of handicap in stroke patients. Stroke. 1988. DOI: 10.1161/01.STR.19.5.604")

RIVERMEAD_ITEMS = [
    "Переворачивание в постели (из положения лёжа на спине на бок)",
    "Переход из положения лёжа в положение сидя",
    "Равновесие сидя (сидит на краю кровати 10 секунд)",
    "Переход из положения сидя в положение стоя",
    "Стояние без поддержки 10 секунд",
    "Пересаживание с кровати на стул и обратно",
    "Ходьба в помещении (с вспомогательным средством при необходимости)",
    "Подъём и спуск по лестнице",
    "Ходьба вне помещения по ровной поверхности",
    "Ходьба внутри помещения без вспомогательных средств",
    "Поднятие предмета с пола из положения стоя",
    "Ходьба вне помещения по неровной поверхности",
    "Купание (входит в ванну/душ и выходит без присмотра)",
    "Подъём и спуск по 4 ступеням без поручня",
    "Бег (или быстрая ходьба) 10 метров без прихрамывания",
]
RIVERMEAD_SOURCE = ("Collen F.M. et al. The Rivermead Mobility Index: a further development "
                    "of the Rivermead Motor Assessment. Clin Rehabil. 1991. "
                    "DOI: 10.1177/026921559100050208")

SHRM_SCORES = {
    0: ("Отсутствие нарушений", "Функции и структуры организма сохранены полностью.",
        "Медицинская реабилитация не показана. Диспансерное наблюдение по месту жительства."),
    1: ("Симптомы без ограничения жизнедеятельности",
        "Пациент может вернуться к прежнему образу жизни и прежнему уровню активности.",
        "Медицинская реабилитация не показана. Профилактические рекомендации, наблюдение."),
    2: ("Лёгкое нарушение функционирования",
        "Не выполняет отдельные виды деятельности с прежней активностью, "
        "но справляется без посторонней помощи; полностью самостоятелен.",
        "Третий этап МР — амбулаторно-поликлинический."),
    3: ("Умеренное нарушение функционирования",
        "Передвигается и обслуживает себя самостоятельно, но нуждается в помощи "
        "при сложных видах активности (приготовление пищи, уборка, магазин).",
        "Третий этап МР — амбулаторный; возможен второй этап (дневной стационар)."),
    4: ("Выраженное нарушение функционирования",
        "Ограниченное самообслуживание, нуждается в ухаживающем; "
        "ограничение возможностей передвижения.",
        "Второй этап МР (дневной стационар) или первый этап (стационар, ОМР); "
        "при невозможности транспортировки — реабилитация на дому."),
    5: ("Грубое нарушение функционирования",
        "Прикован к постели/креслу, полностью зависим, круглосуточно нуждается в уходе.",
        "Первый этап МР — стационар (ОМР) при стабильном состоянии; "
        "при нестабильном — лечение в ОРИТ до стабилизации."),
}
SHRM_SOURCE = ("Приказ Минздрава РФ от 31.07.2020 N 788н «Об утверждении Порядка "
               "организации медицинской помощи по медицинской реабилитации взрослых», "
               "таблица 11 «Шкала реабилитационной маршрутизации (ШРМ)»")

BARTHEL_ITEMS = [
    ("Приём пищи", (0, 5, 10)),
    ("Купание", (0, 5)),
    ("Личная гигиена (умывание, бритьё, чистка зубов)", (0, 5)),
    ("Одевание", (0, 5, 10)),
    ("Контроль дефекации", (0, 5, 10)),
    ("Контроль мочеиспускания", (0, 5, 10)),
    ("Пользование туалетом", (0, 5, 10)),
    ("Пересаживание (кровать — кресло)", (0, 5, 10, 15)),
    ("Передвижение (ходьба/кресло-коляска)", (0, 5, 10, 15)),
    ("Подъём и спуск по лестнице", (0, 5, 10)),
]
BARTHEL_SOURCE = ("Mahoney F.I., Barthel D.W. Functional evaluation: the Barthel Index. "
                  "Md State Med J. 1965. PMID: 14258950")

CALCULATOR_CATALOG = [
    {
        "tool": "modified_rankin_scale",
        "title": "Модифицированная шкала Ранкина (mRS)",
        "domain": "Неврология, реабилитология (инсульт)",
        "description": "Экспертная шкала степени инвалидизации и зависимости в повседневной "
                       "жизни. Детерминированный алгоритм выводит градацию из структурированных "
                       "ответов о симптомах, самостоятельности и способности к ходьбе.",
        "range": "0-6 (целые градации)",
        "units": "баллы (градации)",
        "source": MRS_SOURCE,
    },
    {
        "tool": "rivermead_mobility_index",
        "title": "Индекс мобильности Ривмид (Rivermead Mobility Index)",
        "domain": "Реабилитология, неврология",
        "description": "15 бинарных наблюдений за двигательной активностью пациента "
                       "(«выполняет / не выполняет»). Сумма отражает функциональную мобильность.",
        "range": "0-15 (0 — полная зависимость, 15 — полная мобильность)",
        "units": "баллы",
        "source": RIVERMEAD_SOURCE,
    },
    {
        "tool": "rehab_routing_scale",
        "title": "Шкала реабилитационной маршрутизации (ШРМ)",
        "domain": "Медицинская реабилитация (маршрутизация по этапам МР)",
        "description": "Определяет балл 0-5 по состоянию функционирования пациента и "
                       "рекомендуемый этап медицинской реабилитации.",
        "range": "0-5 (0 — нет нарушений, 5 — грубое нарушение)",
        "units": "баллы",
        "source": SHRM_SOURCE,
    },
    {
        "tool": "barthel_index",
        "title": "Индекс Бартел (Barthel Index)",
        "domain": "Реабилитология (оценка самостоятельности в повседневной жизни)",
        "description": "10 видов повседневной активности с взвешенными баллами; "
                       "сумма отражает степень независимости пациента.",
        "range": "0-100 (0 — полная зависимость, 100 — полная независимость)",
        "units": "баллы",
        "source": BARTHEL_SOURCE,
    },
]


# ---------------------------------------------------------------------------
# Инструменты
# ---------------------------------------------------------------------------

@mcp.tool()
def list_calculators() -> dict:
    """Мета-инструмент: каталог клинических шкал сервера.

    Возвращает для каждой методики клиническое описание, допустимые границы
    параметров, единицы измерения и ссылку на первоисточник.
    """
    log.info("list_calculators")
    return {"calculators": CALCULATOR_CATALOG}


@mcp.tool()
def modified_rankin_scale(
    has_symptoms: Annotated[bool, Field(description="Есть ли симптомы заболевания")],
    can_perform_all_previous_activities: Annotated[bool, Field(
        description="Может ли выполнять ВСЕ прежние обязанности и виды деятельности")],
    independent_without_assistance: Annotated[bool, Field(
        description="Обслуживает ли себя без посторонней помощи")],
    can_walk_without_assistance: Annotated[bool, Field(
        description="Может ли ходить без посторонней поддержки")],
    bedridden_constant_care: Annotated[bool, Field(
        description="Прикован ли к постели и нуждается в постоянном уходе")],
    deceased: Annotated[bool, Field(description="Летальный исход")] = False,
) -> dict:
    """Модифицированная шкала Ранкина (mRS): градация инвалидизации 0-6.

    Алгоритм детерминированно выводит градацию из структурированных ответов —
    модель не «угадывает» балл по свободному тексту.
    """
    if deceased:
        grade = 6
    elif bedridden_constant_care and not can_walk_without_assistance and not independent_without_assistance:
        grade = 5
    elif not can_walk_without_assistance and not independent_without_assistance:
        grade = 4
    elif not independent_without_assistance:
        grade = 3
    elif not can_perform_all_previous_activities:
        grade = 2
    elif has_symptoms:
        grade = 1
    else:
        grade = 0
    if not has_symptoms and grade > 0:
        raise ValueError("Противоречивые ответы: симптомов нет, но указана зависимость от помощи.")
    title, description = MRS_GRADES[grade]
    log.info("modified_rankin_scale -> %s", grade)
    return {
        "scale": "mRS",
        "grade": grade,
        "grade_title": title,
        "description": description,
        "components": {
            "has_symptoms": has_symptoms,
            "can_perform_all_previous_activities": can_perform_all_previous_activities,
            "independent_without_assistance": independent_without_assistance,
            "can_walk_without_assistance": can_walk_without_assistance,
            "bedridden_constant_care": bedridden_constant_care,
        },
        "risk_level": "green" if grade <= 1 else "yellow" if grade <= 3 else "red",
        "source": MRS_SOURCE,
    }


@mcp.tool()
def rivermead_mobility_index(
    answers: Annotated[list[bool], Field(
        description="15 ответов «выполняет (true) / не выполняет (false)» "
                    "строго в порядке пунктов шкалы Ривмид",
        min_length=15, max_length=15)],
) -> dict:
    """Индекс мобильности Ривмид: сумма 15 бинарных наблюдений, итог 0-15."""
    score = sum(1 for answer in answers if answer)
    breakdown = [
        {"item": i + 1, "name": name, "passed": bool(answers[i])}
        for i, name in enumerate(RIVERMEAD_ITEMS)
    ]
    if score <= 4:
        interpretation = "Выраженное снижение мобильности, полная зависимость."
        risk = "red"
    elif score <= 9:
        interpretation = "Умеренное снижение мобильности, требуется помощь."
        risk = "yellow"
    else:
        interpretation = "Сохранная мобильность, высокая самостоятельность."
        risk = "green"
    log.info("rivermead_mobility_index -> %s", score)
    return {
        "scale": "Rivermead Mobility Index",
        "score": score,
        "score_max": 15,
        "interpretation": interpretation,
        "breakdown": breakdown,
        "risk_level": risk,
        "source": RIVERMEAD_SOURCE,
    }


@mcp.tool()
def rehab_routing_scale(
    symptoms_present: Annotated[bool, Field(description="Есть ли симптомы заболевания")],
    previous_activity_possible: Annotated[bool, Field(
        description="Может ли выполнять прежние виды деятельности с прежней активностью")],
    independent_selfcare: Annotated[bool, Field(
        description="Полностью самостоятельное самообслуживание (одевание, туалет, приём пищи)")],
    can_walk_independently: Annotated[bool, Field(
        description="Передвигается самостоятельно без посторонней помощи")],
    needs_help_complex_activities: Annotated[bool, Field(
        description="Нуждается в помощи при сложных видах активности (готовка, уборка, магазин)")],
    constant_care_needed: Annotated[bool, Field(
        description="Круглосуточно нуждается в ухаживающем")],
) -> dict:
    """Шкала реабилитационной маршрутизации (ШРМ): балл 0-5 и этап МР.

    Балл выводится детерминированно из критериев таблицы 11 приложения
    к приказу Минздрава РФ N 788н.
    """
    bedridden = not can_walk_independently and not independent_selfcare
    if bedridden and constant_care_needed:
        score = 5
    elif bedridden or needs_help_complex_activities and not independent_selfcare:
        score = 4
    elif needs_help_complex_activities:
        score = 3
    elif not previous_activity_possible:
        score = 2
    elif symptoms_present:
        score = 1
    else:
        score = 0
    if not symptoms_present and score > 0:
        raise ValueError("Противоречивые ответы: симптомов нет, но указаны ограничения активности.")
    title, description, route = SHRM_SCORES[score]
    log.info("rehab_routing_scale -> %s", score)
    return {
        "scale": "ШРМ",
        "score": score,
        "score_max": 5,
        "score_title": title,
        "description": description,
        "recommended_route": route,
        "risk_level": "green" if score <= 1 else "yellow" if score <= 3 else "red",
        "source": SHRM_SOURCE,
    }


@mcp.tool()
def barthel_index(
    scores: Annotated[list[int], Field(
        description="10 баллов по пунктам шкалы Бартел в порядке: приём пищи (0/5/10), "
                    "купание (0/5), личная гигиена (0/5), одевание (0/5/10), "
                    "контроль дефекации (0/5/10), контроль мочеиспускания (0/5/10), "
                    "пользование туалетом (0/5/10), пересаживание (0/5/10/15), "
                    "передвижение (0/5/10/15), лестница (0/5/10)",
        min_length=10, max_length=10)],
) -> dict:
    """Индекс Бартел: сумма 10 взвешенных пунктов, итог 0-100."""
    breakdown = []
    for i, ((name, allowed), value) in enumerate(zip(BARTHEL_ITEMS, scores)):
        if value not in allowed:
            raise ValueError(
                f"Пункт {i + 1} «{name}»: балл {value} недопустим, разрешены {allowed}."
            )
        breakdown.append({"item": i + 1, "name": name, "score": value})
    total = sum(scores)
    if total <= 20:
        interpretation, risk = "Полная зависимость", "red"
    elif total <= 60:
        interpretation, risk = "Тяжёлая зависимость", "red"
    elif total <= 90:
        interpretation, risk = "Умеренная зависимость", "yellow"
    elif total <= 99:
        interpretation, risk = "Лёгкая зависимость", "yellow"
    else:
        interpretation, risk = "Полная независимость", "green"
    log.info("barthel_index -> %s", total)
    return {
        "scale": "Barthel Index",
        "score": total,
        "score_max": 100,
        "interpretation": interpretation,
        "breakdown": breakdown,
        "risk_level": risk,
        "source": BARTHEL_SOURCE,
    }


if __name__ == "__main__":
    log.info("Сервер rehab-scales запущен (stdio)")
    mcp.run(transport="stdio")
