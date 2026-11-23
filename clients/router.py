"""Задание 4. Единый клиентский роутер трёх MCP-источников для веб-приложения.

Streamlit работает в синхронном цикле, а MCP-клиенты асинхронные — поэтому
роутер держит выделенный event loop в фоновом потоке и исполняет корутины
через asyncio.run_coroutine_threadsafe. Сессии с серверами живут постоянно
(подпроцессы не перезапускаются на каждый клик).

Дополнительно роутер:
    - применяет Sanitizer ко ВСЕМ источникам (единое поведение формы);
    - автоматически поднимает SSE-демон подпроцессом, если порт 8000 закрыт;
    - формирует пакеты JSON-RPC для инспектора протокола.
"""

import asyncio
import itertools
import json
import subprocess
import sys
import threading
from typing import Any

from clients.external_client import ExternalMCPClient
from clients.sse_client import SseMCPClient, sanitize
from clients.stdio_client import StdioMCPClient, ToolError

SOURCES = {
    "stdio": "Локальный stdio",
    "sse": "Локальный сетевой :8000",
    "external": "Внешний сервис",
}


class ClientManager:
    """Держит по одной живой MCP-сессии на источник в фоновом event loop."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._clients: dict[str, Any] = {}
        self._sse_daemon: subprocess.Popen | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Внутренние helpers
    # ------------------------------------------------------------------

    def _run(self, coro, timeout: float = 120.0):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    async def _connect(self, source: str):
        if source == "stdio":
            client = StdioMCPClient()
        elif source == "sse":
            client = SseMCPClient(attempts=2, backoff=0.5)
        elif source == "external":
            client = ExternalMCPClient()
        else:
            raise ValueError(f"неизвестный источник: {source}")
        await client.__aenter__()
        return client

    # ------------------------------------------------------------------
    # Публичный синхронный API для Streamlit
    # ------------------------------------------------------------------

    def ensure(self, source: str) -> tuple[bool, str]:
        """Гарантирует живое подключение; возвращает (успех, сообщение)."""
        if source in self._clients:
            return True, "подключено"
        try:
            self._clients[source] = self._run(self._connect(source))
            return True, "подключено"
        except Exception as first_error:
            if source != "sse":
                return False, str(first_error)
            # SSE-демон не запущен — поднимаем и пробуем ещё раз
            self._sse_daemon = subprocess.Popen(
                [sys.executable, "-m", "servers.sse_server"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                self._clients[source] = self._run(self._connect(source))
                return True, "SSE-демон запущен автоматически"
            except Exception as second_error:
                return False, (
                    "не удалось подключиться к :8000 "
                    f"(запущен автоматически, но не отвечает): {second_error}"
                )

    def list_tools(self, source: str) -> list[dict]:
        ok, message = self.ensure(source)
        if not ok:
            raise ToolError(tool=source, message=message)
        return self._run(self._clients[source].list_tools())

    def call(self, source: str, tool: str, raw_arguments: dict) -> dict:
        """Вызов инструмента. Возвращает dict с результатом, заметками
        санитайзера и пакетами JSON-RPC для инспектора."""
        ok, message = self.ensure(source)
        if not ok:
            raise ToolError(tool=source, message=message)
        client = self._clients[source]
        request_id = next(self._ids)

        tools = {t["name"]: t for t in self._run(client.list_tools())}
        if tool not in tools:
            raise ToolError(tool=tool, message="инструмент не найден на сервере")
        arguments, notes = sanitize(tools[tool]["schema"], raw_arguments)

        request_packet = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        if source == "stdio" or source == "sse":
            result = self._run(client.call_tool(tool, arguments))
            if isinstance(result, tuple):  # sse_client возвращает (result, notes)
                result = result[0]
        else:
            result, from_cache = self._run(client.call_tool(tool, arguments))
            notes.append("ответ из локального кэша" if from_cache else "ответ по сети")
        result_packet = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"content": result, "isError": False},
        }
        return {
            "result": result,
            "notes": notes,
            "packets": {"request": request_packet, "response": result_packet},
        }

    def shutdown(self) -> None:
        for client in self._clients.values():
            try:
                self._run(client.__aexit__(None, None, None), timeout=10)
            except Exception:
                pass
        self._clients.clear()
        if self._sse_daemon:
            self._sse_daemon.terminate()
            self._sse_daemon = None
        self._loop.call_soon_threadsafe(self._loop.stop)


def packets_json(packets: dict) -> tuple[str, str]:
    """Красиво печатает пару пакетов для инспектора."""
    return (
        json.dumps(packets["request"], ensure_ascii=False, indent=2),
        json.dumps(packets["response"], ensure_ascii=False, indent=2),
    )
