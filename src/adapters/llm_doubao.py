import os
import asyncio
import httpx
from typing import Dict, Any

from dotenv import load_dotenv


load_dotenv()
from src.utils.logger import log


class DoubaoClient:
    def __init__(self):
        base = os.getenv('ARK_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3')
        path = os.getenv('ARK_CHAT_PATH', '/chat/completions')
        self.endpoint = base.rstrip('/') + path
        self.api_key = os.getenv('ARK_API_KEY')
        self.model_id = os.getenv('ARK_MODEL_ID')
        self.http_proxy = os.getenv('HTTP_PROXY', '')

    def _proxies_if_reachable(self):
        if not self.http_proxy:
            return None
        try:
            import socket, urllib.parse
            u = urllib.parse.urlparse(self.http_proxy)
            host = u.hostname or '127.0.0.1'
            port = u.port or (80 if u.scheme == 'http' else 443)
            with socket.create_connection((host, port), timeout=0.8):
                return {
                    'http://': self.http_proxy,
                    'https://': self.http_proxy,
                }
        except Exception:
            return None

    async def complete(self, messages: list[Dict[str, str]], temperature: float = 0.2) -> Dict[str, Any]:
        headers = {
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json',
        }
        payload = {
            'model': self.model_id,
            'messages': messages,
            'temperature': temperature,
            'stream': False,
        }
        # Respect HTTP proxy if configured to improve connectivity in restricted networks
        # 更长的读写超时以适配 ARK 生成较慢场景
        # 统一更长的网络超时策略：connect=30s, read/write=120s，并启用 http2
        client_kwargs = {
            'timeout': httpx.Timeout(120.0, connect=30.0, read=120.0, write=120.0),
            'verify': False,
            'trust_env': False,
            'http2': True,
        }
        proxies = self._proxies_if_reachable()
        if proxies:
            client_kwargs['proxies'] = proxies
            await log(f"[豆包ARK] 代理启用: {proxies}")
        else:
            await log("[豆包ARK] 直连请求")

        async with httpx.AsyncClient(**client_kwargs) as client:
            last_exc = None
            for attempt in range(3):
                try:
                    resp = await client.post(self.endpoint, headers=headers, json=payload)
                    resp.raise_for_status()
                    return resp.json()
                except httpx.ReadTimeout as e:
                    await log(f"[豆包ARK] 超时，重试({attempt+1}/3)…")
                    last_exc = e
                except httpx.HTTPError as e:
                    cause = getattr(e, '__cause__', None)
                    await log(f"[豆包ARK] 网络异常：{e.__class__.__name__}: {e}; cause={cause}，重试({attempt+1}/3)…")
                    last_exc = e
                except Exception as e:
                    await log(f"[豆包ARK] 异常：{e}")
                    last_exc = e
                await asyncio.sleep(1 * (2 ** attempt))
            if last_exc:
                raise last_exc