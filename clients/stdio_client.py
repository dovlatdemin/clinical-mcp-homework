"""Задание 1. Клиентский коннектор к локальному MCP-серверу по транспорту stdio.

Управляет жизненным циклом серверного подпроцесса через асинхронный контекстный
менеджер SDK stdio_client: запуск, initialize, tools/list, tools/call и
корректное закрытие каналов при выходе из контекста.

Ошибки сервера (ValueError в инструментах) приходят как McpError и
преобразуются в структурированный ToolError, понятный пользователю.
"""

import asyncio
from dataclasses import dataclass
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError


@dataclass
class ToolError(Exception):
    """Структурированная ошибка вызова клинического инструмента."""

    tool: str
    message: str
    code: int | None = None

    def __str__(self) -> str:
        return f"Инструмент «{self.tool}» отклонил параметры: {self.message}"


class StdioMCPClient:
    """Асинхронный клиент MCP-сервера, запущенного как дочерний процесс."""

    def __init__(self, module: str = "servers.stdio_server"):
        # Сервер стартует как подпроцесс: python -m servers.stdio_server
        self.params = StdioServerParameters(command=sys.executable, args=["-m", module])
        self._stack = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> "StdioMCPClient":
        from contextlib import AsyncExitStack

        self._stack = AsyncExitStack()
        read, write = await self._stack.enter_async_context(stdio_client(self.params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()  # рукопожатие JSON-RPC initialize
        return self

    async def __aexit__(self, *exc_info) -> None:
        # Закрытие каналов и завершение подпроцесса (shutdown)
        await self._stack.aclose()

    async def list_tools(self) -> list[dict]:
        """tools/list: каталог инструментов с JSON-схемами параметров."""
        result = await self.session.list_tools()
        return [
            {"name": tool.name, "description": tool.description, "schema": tool.inputSchema}
            for tool in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """tools/call: вызов инструмента; ValueError сервера -> ToolError."""
        try:
            result = await self.session.call_tool(name, arguments)
        except McpError as error:
            raise ToolError(tool=name, message=error.error.message, code=error.error.code) from error
        if result.isError:
            text = " ".join(block.text for block in result.content if getattr(block, "text", ""))
            raise ToolError(tool=name, message=text.strip() or "неизвестная ошибка сервера")
        if result.structuredContent is not None:
            return result.structuredContent
        # Запасной вариант: сервер вернул JSON текстовым блоком
        import json

        for block in result.content:
            text = getattr(block, "text", "")
            if text:
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"content": text}
        return {}


async def demo() -> None:
    """Сквозная проверка: initialize -> tools/list -> tools/call -> ошибка -> shutdown."""
    async with StdioMCPClient() as client:
        tools = await client.list_tools()
        print(f"Инструменты сервера ({len(tools)}):", ", ".join(t["name"] for t in tools))

        catalog = await client.call_tool("list_calculators", {})
        print(f"Каталог методик: {len(catalog['calculators'])} шт.")

        mrs = await client.call_tool("modified_rankin_scale", {
            "has_symptoms": True,
            "can_perform_all_previous_activities": False,
            "independent_without_assistance": True,
            "can_walk_without_assistance": True,
            "bedridden_constant_care": False,
        })
        print(f"mRS: {mrs['grade']} — {mrs['grade_title']} (риск: {mrs['risk_level']})")

        rivermead = await client.call_tool("rivermead_mobility_index", {
            "answers": [True] * 10 + [False] * 5,
        })
        print(f"Ривмид: {rivermead['score']}/{rivermead['score_max']} — {rivermead['interpretation']}")

        shrm = await client.call_tool("rehab_routing_scale", {
            "symptoms_present": True,
            "previous_activity_possible": False,
            "independent_selfcare": True,
            "can_walk_independently": True,
            "needs_help_complex_activities": True,
            "constant_care_needed": False,
        })
        print(f"ШРМ: {shrm['score']}/{shrm['score_max']} — {shrm['recommended_route']}")

        # Проверка изоляции ToolError: противоречивые параметры
        try:
            await client.call_tool("modified_rankin_scale", {
                "has_symptoms": False,
                "can_perform_all_previous_activities": True,
                "independent_without_assistance": False,
                "can_walk_without_assistance": False,
                "bedridden_constant_care": True,
            })
        except ToolError as error:
            print(f"ToolError перехвачена корректно: {error}")


if __name__ == "__main__":
    asyncio.run(demo())
