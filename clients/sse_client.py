"""Задание 2. Асинхронный клиент сетевого MCP-сервера (SSE, порт 8000).

Возможности:
    - подключение через mcp.client.sse к http://127.0.0.1:8000/sse;
    - пул повторных попыток при кратковременной недоступности порта
      (экспоненциальная задержка между попытками);
    - Sanitizer: автоматическое приведение типов и подстановка
      физиологически безопасных значений по умолчанию (Smart Defaults),
      чтобы форма не падала на пустых или «строковых» значениях;
    - преобразование ошибок сервера в структурированный ToolError.
"""

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
import json
import sys
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.shared.exceptions import McpError

from clients.stdio_client import ToolError  # единый тип ошибки для всех транспортов

# Физиологически безопасные значения по умолчанию для типовых параметров,
# если пользователь оставил поле пустым.
SMART_DEFAULTS: dict[str, Any] = {
    "age": 60,
    "weight_kg": 70.0,
    "height_cm": 170.0,
    "creatinine_umol": 90.0,
    "sex": "male",
    "respiratory_rate": 16,
    "sbp": 120,
}

TRUE_WORDS = {"true", "1", "да", "yes", "on", "y", "д"}
FALSE_WORDS = {"false", "0", "нет", "no", "off", "n", "н"}


def _coerce(value: Any, spec: dict, name: str, notes: list[str]) -> Any:
    """Приведение одного значения к типу из JSON-схемы инструмента."""
    kind = spec.get("type")
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        word = str(value).strip().lower()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
        raise ValueError(f"{name}: не могу понять «{value}» как да/нет")
    if kind in ("integer", "number"):
        try:
            number = float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            raise ValueError(f"{name}: «{value}» не число") from None
        minimum, maximum = spec.get("minimum"), spec.get("maximum")
        if minimum is not None and number < minimum or maximum is not None and number > maximum:
            clipped = min(max(number, minimum if minimum is not None else number),
                          maximum if maximum is not None else number)
            notes.append(f"{name}: {number:g} приведено к границе [{minimum}; {maximum}] → {clipped:g}")
            number = clipped
        return int(number) if kind == "integer" else number
    if "enum" in spec:
        word = str(value).strip().lower()
        for option in spec["enum"]:
            if word == str(option).lower():
                return option
        raise ValueError(f"{name}: «{value}» вне допустимых значений {spec['enum']}")
    if kind == "array":
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",")]
        return [_coerce(item, spec.get("items", {}), name, notes) for item in value]
    return value


def sanitize(schema: dict, raw: dict) -> tuple[dict, list[str]]:
    """Автосанитизация входных данных под JSON-схему инструмента.

    Возвращает (очищенные параметры, список заметок о подстановках).
    Пустые поля заполняются Smart Defaults или default из схемы.
    """
    clean: dict = {}
    notes: list[str] = []
    props = schema.get("properties", {})
    required = set(schema.get("required", []))
    for name, spec in props.items():
        value = raw.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            if "default" in spec:
                clean[name] = spec["default"]
                notes.append(f"{name}: подставлено значение схемы по умолчанию ({spec['default']})")
            elif name in SMART_DEFAULTS:
                clean[name] = SMART_DEFAULTS[name]
                notes.append(f"{name}: подставлено безопасное значение по умолчанию ({SMART_DEFAULTS[name]})")
            elif name in required:
                raise ValueError(f"{name}: обязательное поле не заполнено")
            continue
        clean[name] = _coerce(value, spec, name, notes)
    return clean, notes


@dataclass
class SseMCPClient:
    """Клиент SSE-сервера с пулом повторных попыток подключения."""

    url: str = "http://127.0.0.1:8000/sse"
    attempts: int = 5
    backoff: float = 0.8
    session: ClientSession | None = field(default=None, init=False)
    _stack: AsyncExitStack | None = field(default=None, init=False)

    async def __aenter__(self) -> "SseMCPClient":
        last_error: Exception | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                self._stack = AsyncExitStack()
                read, write = await self._stack.enter_async_context(sse_client(self.url))
                self.session = await self._stack.enter_async_context(ClientSession(read, write))
                await self.session.initialize()
                return self
            except Exception as error:  # порт недоступен, сервер ещё встаёт
                last_error = error
                if self._stack:
                    await self._stack.aclose()
                if attempt < self.attempts:
                    await asyncio.sleep(self.backoff * attempt)
        raise ConnectionError(
            f"Сервер {self.url} недоступен после {self.attempts} попыток: {last_error}"
        ) from last_error

    async def __aexit__(self, *exc_info) -> None:
        if self._stack:
            await self._stack.aclose()

    async def list_tools(self) -> list[dict]:
        result = await self.session.list_tools()
        return [
            {"name": tool.name, "description": tool.description, "schema": tool.inputSchema}
            for tool in result.tools
        ]

    async def call_tool(self, name: str, raw_arguments: dict) -> tuple[dict, list[str]]:
        """tools/call с санитизацией параметров. Возвращает (результат, заметки)."""
        tools = {tool["name"]: tool for tool in await self.list_tools()}
        if name not in tools:
            raise ToolError(tool=name, message="инструмент не найден на сервере")
        arguments, notes = sanitize(tools[name]["schema"], raw_arguments)
        try:
            result = await self.session.call_tool(name, arguments)
        except McpError as error:
            raise ToolError(tool=name, message=error.error.message, code=error.error.code) from error
        if result.isError:
            text = " ".join(block.text for block in result.content if getattr(block, "text", ""))
            raise ToolError(tool=name, message=text.strip() or "неизвестная ошибка сервера")
        if result.structuredContent is not None:
            return result.structuredContent, notes
        for block in result.content:
            text = getattr(block, "text", "")
            if text:
                try:
                    return json.loads(text), notes
                except json.JSONDecodeError:
                    return {"content": text}, notes
        return {}, notes


async def demo() -> None:
    """Сквозная проверка: сервер поднимается подпроцессом, клиент подключается с retry."""
    import subprocess

    server = subprocess.Popen(
        [sys.executable, "-m", "servers.sse_server"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with SseMCPClient() as client:
            tools = await client.list_tools()
            print(f"Инструменты SSE-сервера ({len(tools)}):", ", ".join(t["name"] for t in tools))

            # Строковые и пустые значения — как из небрежно заполненной веб-формы
            messy_form = {
                "creatinine_umol": "132",   # строка вместо числа
                "age": "",                  # пусто → Smart Default 60
                "sex": "MALE",              # регистр → enum
                "weight_kg": "74,5",        # запятая → точка
            }
            result, notes = await client.call_tool("cockcroft_gault", messy_form)
            print(f"Кокрофт-Голт: {result['value']} {result['units']} — {result['interpretation']}")
            for note in notes:
                print(f"  sanitizer: {note}")

            # Выход за физиологические границы — сервер предупреждает, но не падает
            result, _ = await client.call_tool("body_mass_index", {"weight_kg": 260, "height_cm": 175})
            print(f"ИМТ: {result['value']} — {result['category']} (риск: {result['risk_level']})")
            for warning in result["warnings"]:
                print(f"  warning сервера: {warning}")

            # Компоненты GCS вне диапазона — мягкое приведение
            result, _ = await client.call_tool("glasgow_coma_scale",
                                               {"eye_opening": 9, "verbal_response": 3, "motor_response": 5})
            print(f"GCS: {result['score']}/15 — {result['interpretation']}")
            for warning in result["warnings"]:
                print(f"  warning сервера: {warning}")

            catalog, _ = await client.call_tool("list_scales", {})
            print("Нозологии сервера:", ", ".join(catalog["nosologies"]))
    finally:
        server.terminate()
        server.wait(timeout=10)


if __name__ == "__main__":
    asyncio.run(demo())
