"""Задание 4. Веб-интерфейс клинических MCP-инструментов + инспектор JSON-RPC.

Запуск из корня homework_mcp:

    streamlit run app.py

Компоненты (по требованиям задания):
    - переключатель источника: stdio / SSE :8000 / внешний сервис;
    - поиск и фильтрация инструментов (русские и латинские названия);
    - генератор клинических форм из JSON-схемы инструмента;
    - карточка результата: главное значение, цветовая индикация риска,
      разбор компонентов, рекомендации, ссылка на первоисточник;
    - инспектор протокола MCP: сырые JSON-RPC запрос и ответ (аккордеон).

Все вызовы обёрнуты в перехват исключений: некорректные данные показывают
понятное сообщение, приложение не падает.
"""

import re

import streamlit as st

from clients.router import ClientManager, SOURCES
from clients.stdio_client import ToolError
from servers.stdio_server import BARTHEL_ITEMS, RIVERMEAD_ITEMS

st.set_page_config(page_title="Клинические MCP-инструменты", page_icon="🩺", layout="wide")

RISK_STYLE = {
    "green": ("🟢 Низкий риск / благоприятный результат", st.success),
    "yellow": ("🟡 Промежуточный риск — требуется внимание", st.warning),
    "red": ("🔴 Высокий риск — требуется вмешательство", st.error),
}


@st.cache_resource
def get_manager() -> ClientManager:
    return ClientManager()


manager = get_manager()

# ---------------------------------------------------------------------------
# Формы
# ---------------------------------------------------------------------------

def render_form(tool: dict) -> dict:
    """Строит поля ввода по JSON-схеме инструмента. Возвращает сырые значения."""
    name = tool["name"]
    schema = tool.get("schema") or {}
    props = schema.get("properties", {})
    raw: dict = {}

    if name == "rivermead_mobility_index":
        st.caption("Отметьте, что пациент выполняет самостоятельно:")
        answers = [st.checkbox(item, key=f"{name}:rmi:{i}") for i, item in enumerate(RIVERMEAD_ITEMS)]
        raw["answers"] = answers
        return raw
    if name == "barthel_index":
        st.caption("Выберите балл по каждому пункту шкалы Бартел:")
        scores = []
        for i, (item, allowed) in enumerate(BARTHEL_ITEMS):
            scores.append(st.selectbox(item, allowed, index=len(allowed) - 1, key=f"{name}:bi:{i}"))
        raw["scores"] = scores
        return raw

    for param, spec in props.items():
        label = f"{param}" + (f" — {spec['description']}" if spec.get("description") else "")
        key = f"{name}:{param}"
        kind = spec.get("type")
        if kind == "boolean":
            raw[param] = st.checkbox(label, value=bool(spec.get("default", False)), key=key)
        elif "enum" in spec:
            default = spec.get("default", spec["enum"][0])
            raw[param] = st.selectbox(label, spec["enum"],
                                      index=spec["enum"].index(default) if default in spec["enum"] else 0,
                                      key=key)
        elif kind == "integer":
            minimum, maximum = spec.get("minimum"), spec.get("maximum")
            if minimum is not None and maximum is not None and maximum - minimum <= 30:
                raw[param] = st.slider(label, int(minimum), int(maximum),
                                       int(spec.get("default", minimum)), key=key)
            else:
                raw[param] = st.number_input(label, value=int(spec.get("default", 0)), step=1, key=key)
        elif kind == "number":
            raw[param] = st.number_input(label, value=float(spec.get("default", 0.0)),
                                         step=0.5, key=key)
        else:
            raw[param] = st.text_input(label, key=key)
    return raw


# ---------------------------------------------------------------------------
# Карточка результата
# ---------------------------------------------------------------------------

def _doi_link(source: str) -> str | None:
    match = re.search(r"10\.\d{4,}/[^\s,]+", source)
    return f"https://doi.org/{match.group(0)}" if match else None


def render_result(result: dict, notes: list[str]) -> None:
    # Внешний сервис: текстовый контент (markdown)
    if "content" in result and isinstance(result["content"], list):
        st.markdown("### Результат внешнего сервиса")
        for block in result["content"]:
            st.markdown(block)
        return

    risk = result.get("risk_level", "green")
    title, banner = RISK_STYLE.get(risk, RISK_STYLE["green"])

    main_value = result.get("score", result.get("grade", result.get("value")))
    units = result.get("units", "")
    score_max = result.get("score_max")
    if main_value is not None:
        suffix = f" / {score_max}" if score_max is not None else ""
        st.markdown(f"## {result.get('scale', 'Шкала')}: **{main_value}{suffix}** {units}")
    for field_name in ("grade_title", "score_title", "category", "stage"):
        if result.get(field_name):
            st.markdown(f"**{result[field_name]}**")
    banner(title)
    for field_name in ("description", "interpretation", "recommendation", "recommended_route"):
        if result.get(field_name):
            st.write(result[field_name])

    for warning in result.get("warnings", []):
        st.warning(f"⚠️ {warning}")
    for note in notes:
        st.caption(f"🧹 sanitizer: {note}")

    if result.get("components"):
        st.markdown("**Компоненты расчёта**")
        st.table({"компонент": list(result["components"].keys()),
                  "балл": list(result["components"].values())})
    if result.get("breakdown"):
        st.markdown("**Разбор по пунктам**")
        st.table(result["breakdown"])

    if result.get("source"):
        link = _doi_link(result["source"])
        st.caption(f"Первоисточник: {result['source']}")
        if link:
            st.link_button("Открыть DOI", link)


# ---------------------------------------------------------------------------
# Интерфейс
# ---------------------------------------------------------------------------

st.title("🩺 Клинические MCP-инструменты")
st.caption("Все вычисления выполняют MCP-серверы, интерфейс ничего не считает сам.")

if "packets" not in st.session_state:
    st.session_state.packets = []

source_label = st.radio("Источник MCP", list(SOURCES.values()), horizontal=True)
source = next(key for key, value in SOURCES.items() if value == source_label)

try:
    ok, message = manager.ensure(source)
except Exception as error:  # не даём приложению упасть
    ok, message = False, str(error)
if not ok:
    st.error(f"Источник недоступен: {message}")
    if source == "sse":
        st.info("Можно запустить вручную: `python -m servers.sse_server`")
    st.stop()
st.success(f"Подключено: {source_label} ({message})", icon="🔌")

try:
    tools = manager.list_tools(source)
except Exception as error:
    st.error(f"Не удалось получить список инструментов: {error}")
    st.stop()

query = st.text_input("🔍 Поиск инструмента", placeholder="Ранкин, Бартел, СКФ, PubMed, coma...")
filtered = [t for t in tools
            if query.lower() in f"{t['name']} {t.get('description') or ''}".lower()]
if not filtered:
    st.info("По запросу ничего не найдено.")
    st.stop()

tool_name = st.selectbox("Инструмент", [t["name"] for t in filtered])
tool = next(t for t in filtered if t["name"] == tool_name)
st.caption(tool.get("description") or "")

with st.form(key=f"form:{tool_name}"):
    raw = render_form(tool)
    submitted = st.form_submit_button("Выполнить расчёт", type="primary")

if submitted:
    try:
        outcome = manager.call(source, tool_name, raw)
        st.session_state.packets.append(outcome["packets"])
        render_result(outcome["result"], outcome["notes"])
    except ToolError as error:
        st.error(f"Инструмент отклонил ввод: {error}")
    except Exception as error:
        st.error(f"Неожиданная ошибка (перехвачена, приложение работает): {error}")

# ---------------------------------------------------------------------------
# Инспектор протокола MCP
# ---------------------------------------------------------------------------

with st.expander(f"🛠 Инспектор протокола MCP — пакетов: {len(st.session_state.packets)}"):
    if not st.session_state.packets:
        st.caption("Выполните расчёт — здесь появятся сырые JSON-RPC пакеты.")
    for i, pair in enumerate(reversed(st.session_state.packets[-5:])):
        n = len(st.session_state.packets) - i
        col_req, col_res = st.columns(2)
        with col_req:
            st.markdown(f"**Запрос #{n}** (`tools/call`)")
            st.json(pair["request"])
        with col_res:
            st.markdown(f"**Ответ #{n}** (`result`)")
            st.json(pair["response"])
