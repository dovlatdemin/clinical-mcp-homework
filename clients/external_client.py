"""Задание 3. Клиент внешнего распределённого MCP-сервера доказательной медицины.

Источник по умолчанию — публичный пакет medical-mcp из реестра npm
(инструменты OpenFDA, PubMed, DailyMed, RxNorm; github.com/JamesANZ/medical-mcp).
Сервер поднимается командой `npx -y medical-mcp` и обслуживается по протоколу
MCP через stdio — вызовы идут строго через протокол, не напрямую в API.

Альтернатива: задать переменную окружения EXTERNAL_MCP_URL, например
публичный Streamable HTTP эндпоинт из каталога Glama.ai
(https://openfda.caseyjhand.com/mcp) — тогда клиент работает по сети.

Особенности внешнего источника, требуемые заданием:
    - таймаут вызова 12 секунд: интерфейс не зависает при сетевых задержках;
    - файловый кэш результатов (TTL 24 часа) — защита от Rate Limits внешних API;
    - ошибки преобразуются в структурированный ToolError.
"""

import asyncio
from contextlib import AsyncExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

from clients.stdio_client import ToolError

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache" / "external"
CACHE_TTL = 24 * 3600          # секунд; кэш суточный — защита от Rate Limits
CALL_TIMEOUT = 12.0            # таймаут вызова внешнего инструмента, секунд
STARTUP_TIMEOUT = 90.0         # первый запуск npx скачивает пакет


def _node_command() -> str:
    """Путь к настоящему node.exe.

    В PATH могут быть .cmd-шимы (Node Version Manager и т.п.), которые не
    пробрасывают stdin/stdout подпроцессу — предпочитаем настоящий exe.
    """
    if sys.platform == "win32":
        candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("node") or "node"


def _cache_key(tool: str, arguments: dict) -> Path:
    digest = hashlib.sha256(
        json.dumps({"tool": tool, "args": arguments}, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def cache_read(tool: str, arguments: dict) -> dict | None:
    path = _cache_key(tool, arguments)
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - record.get("ts", 0) > CACHE_TTL:
        return None
    return record["result"]


def cache_write(tool: str, arguments: dict, result: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_key(tool, arguments)
    path.write_text(
        json.dumps({"ts": time.time(), "tool": tool, "result": result}, ensure_ascii=False),
        encoding="utf-8",
    )


class ExternalMCPClient:
    """Клиент внешнего MCP-сервера: npx medical-mcp (stdio) или удалённый URL."""

    def __init__(self) -> None:
        self.remote_url = os.environ.get("EXTERNAL_MCP_URL")
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> "ExternalMCPClient":
        self._stack = AsyncExitStack()
        if self.remote_url:
            # Удалённый Streamable HTTP эндпоинт (каталог Glama.ai и т.п.)
            from mcp.client.streamable_http import streamablehttp_client

            read, write, _ = await self._stack.enter_async_context(
                streamablehttp_client(self.remote_url)
            )
        else:
            # Локальная установка: npm install medical-mcp (без загрузки браузера:
            # PUPPETEER_SKIP_DOWNLOAD=true). Запуск как в официальном README пакета.
            server_js = (Path(__file__).resolve().parent.parent
                         / "node_modules" / "medical-mcp" / "build" / "index.js")
            params = StdioServerParameters(
                command=_node_command(),
                args=[str(server_js)],
                env={"PUPPETEER_SKIP_DOWNLOAD": "true"},
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(self.session.initialize(), timeout=STARTUP_TIMEOUT)
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._stack:
            await self._stack.aclose()

    async def list_tools(self) -> list[dict]:
        result = await asyncio.wait_for(self.session.list_tools(), timeout=CALL_TIMEOUT)
        return [
            {"name": tool.name, "description": tool.description, "schema": tool.inputSchema}
            for tool in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict, *, use_cache: bool = True) -> tuple[dict, bool]:
        """tools/call с таймаутом и кэшем. Возвращает (результат, из_кэша)."""
        if use_cache:
            cached = cache_read(name, arguments)
            if cached is not None:
                return cached, True
        try:
            result = await asyncio.wait_for(
                self.session.call_tool(name, arguments), timeout=CALL_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise ToolError(
                tool=name,
                message=f"внешний сервер не ответил за {CALL_TIMEOUT:g} с "
                        "(сетевая задержка). Попробуйте позже.",
            ) from None
        except McpError as error:
            raise ToolError(tool=name, message=error.error.message, code=error.error.code) from error
        if result.isError:
            text = " ".join(block.text for block in result.content if getattr(block, "text", ""))
            raise ToolError(tool=name, message=text.strip() or "ошибка внешнего сервера")
        payload: dict = {"content": [getattr(b, "text", "") for b in result.content]}
        if result.structuredContent is not None:
            payload["structured"] = result.structuredContent
        cache_write(name, arguments, payload)
        return payload, False


async def demo() -> None:
    """Поиск в PubMed через внешний сервер: сеть → кэш → повтор из кэша."""
    async with ExternalMCPClient() as client:
        tools = await client.list_tools()
        names = [t["name"] for t in tools]
        print(f"Внешние инструменты ({len(names)}):", ", ".join(names[:12]), "...")

        pubmed = next((n for n in names if "pubmed" in n.lower() or "literature" in n.lower()), None)
        if not pubmed:
            print("PubMed-инструмент не найден у внешнего сервера.")
            return
        query = {"query": "stroke rehabilitation modified Rankin scale", "max_results": 3}

        started = time.monotonic()
        result, from_cache = await client.call_tool(pubmed, query)
        print(f"Первый вызов {pubmed}: из кэша={from_cache}, {time.monotonic() - started:.1f} с")
        print(str(result)[:400], "...")

        started = time.monotonic()
        result, from_cache = await client.call_tool(pubmed, query)
        print(f"Повторный вызов: из кэша={from_cache}, {time.monotonic() - started:.3f} с")


if __name__ == "__main__":
    asyncio.run(demo())
