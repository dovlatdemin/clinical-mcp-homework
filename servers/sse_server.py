"""Задание 2. Локальный сетевой MCP-сервер клинических калькуляторов (SSE, порт 8000).

Запуск из корня homework_mcp:

    python -m servers.sse_server

Сервер слушает http://127.0.0.1:8000:
    GET  /sse                     — инициализация сессии, подписка на события;
    POST /messages?session_id=... — приём запросов JSON-RPC 2.0.

Инструменты сгруппированы по нозологиям (см. list_scales):
    общая терапия     — body_mass_index;
    кардиология       — cha2ds2_vasc;
    нефрология        — ckd_epi_egfr, cockcroft_gault;
    реаниматология    — glasgow_coma_scale, qsofa.

Валидация граничных условий: при выходе показателей за физиологические границы
сервер НЕ падает, а возвращает результат со списком warnings (и мягко
приводит компоненты шкал к допустимому диапазону). Журнал — в stderr.
"""

import logging
import math
import sys
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
log = logging.getLogger("clinical-scales-sse")

mcp = FastMCP("clinical-scales", host="127.0.0.1", port=8000)

# Физиологические границы: выход за них — повод для предупреждения, а не аварии.
PHYSIO_LIMITS = {
    "weight_kg": (2.0, 500.0, "кг"),
    "height_cm": (50.0, 250.0, "см"),
    "age": (18.0, 120.0, "лет"),
    "creatinine_umol": (20.0, 1500.0, "мкмоль/л"),
    "respiratory_rate": (4.0, 60.0, "в мин"),
    "sbp": (40.0, 300.0, "мм рт. ст."),
}


def check_limit(name: str, value: float, warnings: list[str]) -> None:
    low, high, unit = PHYSIO_LIMITS[name]
    if not low <= value <= high:
        warnings.append(
            f"{name}={value:g} вне физиологических границ [{low:g}; {high:g}] {unit}. "
            "Результат расчёта может быть недостоверен — проверьте ввод."
        )


def clamp(value: int, low: int, high: int, name: str, warnings: list[str]) -> int:
    if value < low or value > high:
        warnings.append(f"{name}={value} приведён к допустимому диапазону [{low}; {high}].")
    return max(low, min(high, value))


# ---------------------------------------------------------------------------
# Общая терапия
# ---------------------------------------------------------------------------

@mcp.tool()
def body_mass_index(
    weight_kg: Annotated[float, Field(description="Масса тела, кг")],
    height_cm: Annotated[float, Field(description="Рост, см")],
) -> dict:
    """Индекс массы тела (ИМТ/BMI), кг/м². Нозология: общая терапия."""
    warnings: list[str] = []
    check_limit("weight_kg", weight_kg, warnings)
    check_limit("height_cm", height_cm, warnings)
    if weight_kg <= 0 or height_cm <= 0:
        raise ValueError("Масса и рост должны быть положительными числами.")
    bmi = weight_kg / (height_cm / 100) ** 2
    if bmi < 16:
        category, risk = "Выраженный дефицит массы тела", "red"
    elif bmi < 18.5:
        category, risk = "Дефицит массы тела", "yellow"
    elif bmi < 25:
        category, risk = "Норма", "green"
    elif bmi < 30:
        category, risk = "Избыточная масса тела", "yellow"
    elif bmi < 35:
        category, risk = "Ожирение I степени", "yellow"
    elif bmi < 40:
        category, risk = "Ожирение II степени", "red"
    else:
        category, risk = "Ожирение III степени", "red"
    log.info("body_mass_index -> %.1f", bmi)
    return {
        "scale": "ИМТ (BMI)",
        "nosology": "общая терапия",
        "value": round(bmi, 1),
        "units": "кг/м²",
        "category": category,
        "risk_level": risk,
        "warnings": warnings,
        "source": "WHO. Obesity: preventing and managing the global epidemic. "
                  "WHO Technical Report Series 894, 2000.",
    }


# ---------------------------------------------------------------------------
# Кардиология
# ---------------------------------------------------------------------------

@mcp.tool()
def cha2ds2_vasc(
    age: Annotated[int, Field(description="Возраст, лет")],
    sex: Annotated[Literal["male", "female"], Field(description="Пол: male / female")],
    congestive_heart_failure: Annotated[bool, Field(description="Хроническая сердечная недостаточность")] = False,
    hypertension: Annotated[bool, Field(description="Артериальная гипертензия")] = False,
    diabetes: Annotated[bool, Field(description="Сахарный диабет")] = False,
    stroke_tia_history: Annotated[bool, Field(description="Инсульт/ТИА/тромбоэмболия в анамнезе")] = False,
    vascular_disease: Annotated[bool, Field(description="Сосудистое заболевание (ИМ, ПАЗ, аортальная бляшка)")] = False,
) -> dict:
    """Шкала CHA₂DS₂-VASc: риск ишемического инсульта при фибрилляции предсердий.

    Нозология: кардиология. Диапазон 0-9 баллов.
    """
    warnings: list[str] = []
    check_limit("age", age, warnings)
    components = {
        "ХСН": int(congestive_heart_failure),
        "Гипертензия": int(hypertension),
        "Возраст ≥ 75": 2 if age >= 75 else 0,
        "Диабет": int(diabetes),
        "Инсульт/ТИА в анамнезе": 2 * int(stroke_tia_history),
        "Сосудистое заболевание": int(vascular_disease),
        "Возраст 65-74": 1 if 65 <= age < 75 else 0,
        "Женский пол": int(sex == "female"),
    }
    score = sum(components.values())
    if score <= (0 if sex == "male" else 1):
        recommendation, risk = "Низкий риск: антикоагулянты не показаны.", "green"
    elif score <= (1 if sex == "male" else 2):
        recommendation, risk = "Промежуточный риск: рассмотреть антикоагулянтную терапию.", "yellow"
    else:
        recommendation, risk = "Высокий риск: показана антикоагулянтная терапия (если нет противопоказаний).", "red"
    log.info("cha2ds2_vasc -> %s", score)
    return {
        "scale": "CHA₂DS₂-VASc",
        "nosology": "кардиология",
        "score": score,
        "score_max": 9,
        "components": components,
        "recommendation": recommendation,
        "risk_level": risk,
        "warnings": warnings,
        "source": "Lip G.Y.H. et al. Chest. 2010. DOI: 10.1378/chest.09-1584",
    }


# ---------------------------------------------------------------------------
# Нефрология
# ---------------------------------------------------------------------------

def _ckd_epi(age: int, sex: str, scr_mgdl: float) -> float:
    """Формула CKD-EPI 2021 (без расового коэффициента)."""
    kappa = 0.7 if sex == "female" else 0.9
    alpha = -0.241 if sex == "female" else -0.302
    ratio = scr_mgdl / kappa
    egfr = 142 * min(ratio, 1) ** alpha * max(ratio, 1) ** (-1.200) * 0.9938 ** age
    if sex == "female":
        egfr *= 1.012
    return egfr


def _ckd_stage(egfr: float) -> tuple[str, str]:
    if egfr >= 90:
        return "G1 — нормальная или повышенная СКФ", "green"
    if egfr >= 60:
        return "G2 — слегка сниженная СКФ", "green"
    if egfr >= 45:
        return "G3a — умеренно сниженная СКФ", "yellow"
    if egfr >= 30:
        return "G3b — заметно сниженная СКФ", "yellow"
    if egfr >= 15:
        return "G4 — резко сниженная СКФ", "red"
    return "G5 — терминальная почечная недостаточность", "red"


@mcp.tool()
def ckd_epi_egfr(
    creatinine_umol: Annotated[float, Field(description="Креатинин сыворотки, мкмоль/л")],
    age: Annotated[int, Field(description="Возраст, лет (формула валидирована для ≥ 18)")],
    sex: Annotated[Literal["male", "female"], Field(description="Пол: male / female")],
) -> dict:
    """СКФ по формуле CKD-EPI 2021. Нозология: нефрология. мл/мин/1,73 м²."""
    warnings: list[str] = []
    check_limit("creatinine_umol", creatinine_umol, warnings)
    check_limit("age", age, warnings)
    if creatinine_umol <= 0:
        raise ValueError("Креатинин должен быть положительным числом.")
    egfr = _ckd_epi(age, sex, creatinine_umol / 88.4)
    stage, risk = _ckd_stage(egfr)
    log.info("ckd_epi_egfr -> %.1f", egfr)
    return {
        "scale": "СКФ (CKD-EPI 2021)",
        "nosology": "нефрология",
        "value": round(egfr, 1),
        "units": "мл/мин/1,73 м²",
        "stage": stage,
        "risk_level": risk,
        "warnings": warnings,
        "source": "Inker L.A. et al. N Engl J Med. 2021. DOI: 10.1056/NEJMoa2102953",
    }


@mcp.tool()
def cockcroft_gault(
    creatinine_umol: Annotated[float, Field(description="Креатинин сыворотки, мкмоль/л")],
    age: Annotated[int, Field(description="Возраст, лет")],
    weight_kg: Annotated[float, Field(description="Масса тела, кг")],
    sex: Annotated[Literal["male", "female"], Field(description="Пол: male / female")],
) -> dict:
    """Клиренс креатинина по Кокрофту-Голту. Нозология: нефрология. мл/мин."""
    warnings: list[str] = []
    check_limit("creatinine_umol", creatinine_umol, warnings)
    check_limit("age", age, warnings)
    check_limit("weight_kg", weight_kg, warnings)
    if creatinine_umol <= 0 or weight_kg <= 0:
        raise ValueError("Креатинин и масса должны быть положительными числами.")
    scr_mgdl = creatinine_umol / 88.4
    crcl = (140 - age) * weight_kg / (72 * scr_mgdl)
    if sex == "female":
        crcl *= 0.85
    if crcl >= 90:
        interpretation, risk = "Нормальная функция почек", "green"
    elif crcl >= 60:
        interpretation, risk = "Умеренное снижение", "green"
    elif crcl >= 30:
        interpretation, risk = "Значимое снижение: коррекция доз нефротоксичных препаратов", "yellow"
    else:
        interpretation, risk = "Резкое снижение: консультация нефролога", "red"
    log.info("cockcroft_gault -> %.1f", crcl)
    return {
        "scale": "Клиренс креатинина (Кокрофт-Голт)",
        "nosology": "нефрология",
        "value": round(crcl, 1),
        "units": "мл/мин",
        "interpretation": interpretation,
        "risk_level": risk,
        "warnings": warnings,
        "source": "Cockcroft D.W., Gault M.H. Nephron. 1976. PMID: 1244564",
    }


# ---------------------------------------------------------------------------
# Реаниматология
# ---------------------------------------------------------------------------

@mcp.tool()
def glasgow_coma_scale(
    eye_opening: Annotated[int, Field(description="Открывание глаз: 1-4")],
    verbal_response: Annotated[int, Field(description="Речевая реакция: 1-5")],
    motor_response: Annotated[int, Field(description="Двигательная реакция: 1-6")],
) -> dict:
    """Шкала комы Глазго (GCS): сумма компонентов 3-15. Нозология: реаниматология.

    Компоненты вне диапазона мягко приводятся к границам с предупреждением.
    """
    warnings: list[str] = []
    eye = clamp(eye_opening, 1, 4, "eye_opening", warnings)
    verbal = clamp(verbal_response, 1, 5, "verbal_response", warnings)
    motor = clamp(motor_response, 1, 6, "motor_response", warnings)
    score = eye + verbal + motor
    if score >= 13:
        interpretation, risk = "Лёгкое нарушение сознания", "green"
    elif score >= 9:
        interpretation, risk = "Умеренное нарушение сознания", "yellow"
    else:
        interpretation, risk = "Тяжёлое нарушение сознания (кома): оценить проходимость дыхательных путей", "red"
    log.info("glasgow_coma_scale -> %s", score)
    return {
        "scale": "GCS",
        "nosology": "реаниматология",
        "score": score,
        "score_max": 15,
        "components": {"E": eye, "V": verbal, "M": motor},
        "interpretation": interpretation,
        "risk_level": risk,
        "warnings": warnings,
        "source": "Teasdale G., Jennett B. Lancet. 1974. PMID: 4136544",
    }


@mcp.tool()
def qsofa(
    respiratory_rate: Annotated[int, Field(description="Частота дыхания, в минуту")],
    sbp: Annotated[int, Field(description="Систолическое АД, мм рт. ст.")],
    altered_mentation: Annotated[bool, Field(description="Изменённое сознание (GCS < 15)")],
) -> dict:
    """qSOFA: быстрый скрининг неблагоприятного исхода при подозрении на сепсис.

    Нозология: реаниматология. Диапазон 0-3 балла.
    """
    warnings: list[str] = []
    check_limit("respiratory_rate", respiratory_rate, warnings)
    check_limit("sbp", sbp, warnings)
    components = {
        "ЧД ≥ 22/мин": int(respiratory_rate >= 22),
        "САД ≤ 100 мм рт. ст.": int(sbp <= 100),
        "Изменённое сознание": int(altered_mentation),
    }
    score = sum(components.values())
    if score >= 2:
        interpretation, risk = ("Высокий риск неблагоприятного исхода: расширенная оценка SOFA, "
                                "мониторинг, рассмотреть перевод в ОРИТ"), "red"
    elif score == 1:
        interpretation, risk = "Промежуточный риск: динамическое наблюдение", "yellow"
    else:
        interpretation, risk = "Низкий риск по qSOFA", "green"
    log.info("qsofa -> %s", score)
    return {
        "scale": "qSOFA",
        "nosology": "реаниматология",
        "score": score,
        "score_max": 3,
        "components": components,
        "interpretation": interpretation,
        "risk_level": risk,
        "warnings": warnings,
        "source": "Singer M. et al. JAMA. 2016 (Sepsis-3). DOI: 10.1001/jama.2016.0287",
    }


@mcp.tool()
def list_scales() -> dict:
    """Мета-инструмент: каталог шкал сервера с разбиением по нозологиям."""
    catalog = {
        "общая терапия": ["body_mass_index — ИМТ, кг/м² (18,5-25 норма)"],
        "кардиология": ["cha2ds2_vasc — риск инсульта при ФП, 0-9 баллов"],
        "нефрология": [
            "ckd_epi_egfr — СКФ, мл/мин/1,73 м²; креатинин 20-1500 мкмоль/л, возраст 18-120",
            "cockcroft_gault — клиренс креатинина, мл/мин",
        ],
        "реаниматология": [
            "glasgow_coma_scale — GCS, 3-15; E 1-4, V 1-5, M 1-6",
            "qsofa — скрининг сепсиса, 0-3",
        ],
    }
    log.info("list_scales")
    return {"nosologies": catalog}


if __name__ == "__main__":
    log.info("SSE-сервер clinical-scales: http://127.0.0.1:8000/sse")
    mcp.run(transport="sse")
