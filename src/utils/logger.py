import asyncio
import os
from typing import Set


class LogBus:
    def __init__(self):
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.clients: Set[asyncio.Queue] = set()

    async def publish(self, message: str):
        await self.queue.put(message)

    async def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue()
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]):
        self.clients.discard(q)

    async def broadcaster(self):
        while True:
            msg = await self.queue.get()
            # 可选隐藏：当当前提供方不是 MCP 时，隐藏所有以 [MCP] 开头的日志
            try:
                provider = os.getenv('SEARCH_PROVIDER', 'ddgs').lower()
                hide_mcp = (provider != 'mcp') or (os.getenv('LOG_HIDE_MCP', 'false').lower() == 'true')
                if hide_mcp and (msg.startswith('[MCP]') or '[MCP]' in msg):
                    # 跳过广播 MCP 日志
                    continue
            except Exception:
                pass
            # broadcast to all clients
            for q in list(self.clients):
                try:
                    await q.put(msg)
                except Exception:
                    # drop faulty client
                    self.clients.discard(q)


log_bus = LogBus()

async def log(msg: str):
    # also print to console for debugging when WS is unavailable
    try:
        provider = os.getenv('SEARCH_PROVIDER', 'ddgs').lower()
        hide_mcp = (provider != 'mcp') or (os.getenv('LOG_HIDE_MCP', 'false').lower() == 'true')
        if not (hide_mcp and (msg.startswith('[MCP]') or '[MCP]' in msg)):
            print(msg)
    except Exception:
        pass
    await log_bus.publish(msg)