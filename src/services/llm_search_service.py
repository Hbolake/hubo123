import asyncio
import json
import re
from typing import List, Dict, Tuple, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from readability import Document
import os

from src.utils.logger import log


def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


def _extract_json_array(text: str) -> Optional[List[Dict]]:
    """Try to find a JSON array in the model output and parse it.
    Supports code-fence wrapped JSON or plain text with extra notes.
    """
    if not text:
        return None
    # strip code fences if present (support CRLF and plain fences)
    t = text.strip()
    t = re.sub(r"^```(?:json)?\r?\n", "", t)
    t = re.sub(r"\r?\n```$", "", t)
    text = t
    # find first '[' and last ']' (tolerate truncated arrays)
    m = re.search(r"\[", text)
    n = text.rfind("]")
    if m:
        snippet = text[m.start(): (n+1 if n != -1 else len(text))]
        try:
            data = json.loads(snippet)
            if isinstance(data, list):
                return data
        except Exception:
            # try salvage objects from truncated array by brace balancing
            objs: List[Dict] = []
            buf = []
            depth = 0
            started = False
            for ch in snippet:
                if ch == '{':
                    depth += 1
                    started = True
                if started:
                    buf.append(ch)
                if ch == '}':
                    depth = max(0, depth-1)
                    if depth == 0 and buf:
                        s = ''.join(buf)
                        try:
                            obj = json.loads(s)
                            if isinstance(obj, dict):
                                objs.append(obj)
                        except Exception:
                            pass
                        buf = []
                        started = False
            if objs:
                return objs
    # try whole text
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return None


class LLMSearchService:
    """
    Call AiHubMix LLM with web-search capability to obtain structured evidence list.
    It first tries /v1/chat/completions (messages-style), and falls back to /v1/responses.
    Returned items are normalized to contain at least: href, title, source, date, summary.
    Then we reuse readability-based fetch to get article_text for downstream report.
    """

    def __init__(self, max_results: int = 12, max_fetch_html: int = 8,
                 trusted_domains: Optional[List[str]] = None,
                 only_trusted: bool = False,
                 blacklist_domains: Optional[List[str]] = None,
                 base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 model_id: Optional[str] = None,
                 language: str = "zh",
                 time_days: int = 30):
        self.max_results = max_results
        self.max_fetch_html = max_fetch_html
        self.only_trusted = only_trusted
        self.trusted_domains = [urlparse(d).netloc or d for d in (trusted_domains or [])]
        self.blacklist_domains = [urlparse(d).netloc or d for d in (blacklist_domains or [])]
        # 兼容两种写法：传入 https://aihubmix.com 或 https://aihubmix.com/v1
        raw_base = (base_url or "https://aihubmix.com").rstrip("/")
        self._base_api = raw_base if raw_base.endswith("/v1") else f"{raw_base}/v1"
        self.api_key = api_key or ""
        self.model_id = model_id or "gemini-2.5-pro-search"
        self.language = language
        self.time_days = time_days
        # optional HTTP proxy for restricted networks
        self.http_proxy = os.getenv('HTTP_PROXY', '')

    def _proxies_if_reachable(self) -> Optional[Dict]:
        """Return httpx proxies dict if self.http_proxy is reachable; otherwise None.
        We do a quick TCP port probe to avoid httpx ConnectError: All connection attempts failed.
        """
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
                    'all://': self.http_proxy,
                }
        except Exception:
            return None

    def _rank_results(self, items: List[Dict]) -> List[Dict]:
        trusted, others = [], []
        for it in items:
            dom = domain_of(it.get('href') or it.get('url') or '')
            if dom in self.blacklist_domains:
                continue
            if self.trusted_domains and dom in self.trusted_domains:
                trusted.append(it)
            else:
                others.append(it)
        ranked = trusted + ([] if self.only_trusted else others)
        return ranked[:self.max_results]

    async def _call_chat_completions(self, query: str) -> Optional[List[Dict]]:
        url = f"{self._base_api}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # Minimal prompt (observed stable) — ask for JSON array only
        req_topn = min(self.max_results, 10)
        user_prompt = (
            "请联网搜索并返回JSON数组，每个元素包含title,url,source,date,summary。"
            f"主题：{query}；TopN={req_topn}；时间范围={self.time_days}天；语言={self.language}；"
            f"白名单域名优先：{', '.join(self.trusted_domains)}；排除视频网站与仅视频页（如 YouTube、Bilibili、TikTok、优酷、西瓜视频、Vimeo 等），优先新闻/媒体/博客等文字页面。"
            "只返回JSON，不要附加解释。"
        )

        payload = {
            "model": self.model_id,
            "messages": [
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "max_tokens": 2048,
            # Try common web search options (if unsupported, server can ignore)
            "web_search_options": {
                "enable": True,
                "max_results": self.max_results,
                "time_range_days": self.time_days,
                "language": self.language,
                "domains_whitelist": self.trusted_domains,
                "domains_blacklist": self.blacklist_domains,
            },
        }
        try:
            # retry with exponential backoff for transient network/adaptor errors
            # 更稳健的超时与连接配置：connect=30s, read/write=120s，启用 http2
            client_kwargs = {
                'timeout': httpx.Timeout(120.0, connect=30.0, read=120.0, write=120.0),
                'verify': False,
                'trust_env': False,
                'http2': True,
            }
            proxies = self._proxies_if_reachable()
            if proxies:
                client_kwargs['proxies'] = proxies
                await log(f"[LLM检索] chat/completions 代理启用: {self.http_proxy}")
            elif self.http_proxy:
                await log(f"[LLM检索] 代理不可达，改用直连: {self.http_proxy}")
            async with httpx.AsyncClient(**client_kwargs) as client:
                r = None
                for attempt in range(3):
                    try:
                        r = await client.post(url, headers=headers, json=payload)
                        break
                    except httpx.ReadTimeout:
                        await log(f"[LLM检索] chat/completions 超时，重试({attempt+1}/3)…")
                        await asyncio.sleep(1 * (2 ** attempt))
                    except httpx.HTTPError as e:
                        cause = getattr(e, '__cause__', None)
                        await log(f"[LLM检索] chat/completions 网络异常：{e.__class__.__name__}: {e}; cause={cause}，重试({attempt+1}/3)…")
                        await asyncio.sleep(1 * (2 ** attempt))
                if r is None:
                    await log("[LLM检索] chat/completions 重试后仍失败")
                    return None
                if r.status_code >= 400:
                    try:
                        body = r.text
                    except Exception:
                        body = ""
                    await log(f"[LLM检索] chat/completions 返回 {r.status_code}，body={body[:200]}，将尝试其他端点…")
                    return None
                data = r.json()
                # OpenAI兼容风格: choices[0].message.content
                content = (
                    (data.get('choices') or [{}])[0]
                    .get('message', {})
                    .get('content', '')
                )
                arr = _extract_json_array(content)
                items: List[Dict] = []
                if not arr:
                    # Diagnostic: log brief payload structure to understand provider format
                    try:
                        keys = list(data.keys())
                        await log(f"[LLM检索] chat/completions 无JSON，keys={keys}, content_len={len(content or '')}")
                        body_preview = json.dumps(data)[:300]
                        await log(f"[LLM检索] chat/completions body预览: {body_preview}")
                    except Exception:
                        pass
                    # Fallback: extract URLs from plain text
                    urls = re.findall(r"https?://[\w\-\.\?\=/#%&:+]+", content or "")
                    urls = list(dict.fromkeys(urls))[: self.max_results]
                    for u in urls:
                        items.append({
                            'href': u,
                            'title': '',
                            'source': domain_of(u) or '',
                            'date': '',
                            'summary': '',
                        })
                    if not items:
                        await log("[LLM检索] 未解析到JSON或URL，忽略此端点结果…")
                        return None
                else:
                    for it in arr:
                        if not isinstance(it, dict):
                            continue
                        url0 = it.get('url') or it.get('href') or ''
                        items.append({
                            'href': url0,
                            'title': it.get('title') or '',
                            'source': it.get('source') or (domain_of(url0) or ''),
                            'date': it.get('date') or '',
                            'summary': it.get('summary') or '',
                        })
                return self._rank_results(items)
        except Exception as e:
            err_msg = str(e) if str(e) else e.__class__.__name__
            await log(f"[LLM检索] chat/completions 异常：{err_msg}")
            return None

    async def _call_responses(self, query: str) -> Optional[List[Dict]]:
        url = f"{self._base_api}/responses"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # Many providers accept 'input' or a messages-like structure under responses.
        # We'll provide a simple instruction string.
        req_topn = min(self.max_results, 10)
        instruction = (
            "请联网搜索并返回JSON数组，每个元素包含title,url,source,date,summary。"
            f"主题: {query}; TopN={req_topn}; 时间范围={self.time_days}天; 语言={self.language};"
            f" 白名单域名优先: {', '.join(self.trusted_domains)}；排除视频网站与仅视频页（如 YouTube、Bilibili、TikTok、优酷、西瓜视频、Vimeo 等），优先新闻/媒体/博客等文字页面。"
            "只返回JSON。"
        )
        payload = {
            "model": self.model_id,
            "input": instruction,
            "max_output_tokens": 2048,
        }
        try:
            client_kwargs = {
                'timeout': httpx.Timeout(120.0, connect=30.0, read=120.0, write=120.0),
                'verify': False,
                'trust_env': False,
                'http2': True,
            }
            proxies = self._proxies_if_reachable()
            if proxies:
                client_kwargs['proxies'] = proxies
                await log(f"[LLM检索] responses 代理启用: {self.http_proxy}")
            elif self.http_proxy:
                await log(f"[LLM检索] 代理不可达，改用直连: {self.http_proxy}")
            async with httpx.AsyncClient(**client_kwargs) as client:
                r = None
                for attempt in range(3):
                    try:
                        r = await client.post(url, headers=headers, json=payload)
                        break
                    except httpx.ReadTimeout:
                        await log(f"[LLM检索] responses 超时，重试({attempt+1}/3)…")
                        await asyncio.sleep(1 * (2 ** attempt))
                    except httpx.HTTPError as e:
                        cause = getattr(e, '__cause__', None)
                        await log(f"[LLM检索] responses 网络异常：{e.__class__.__name__}: {e}; cause={cause}，重试({attempt+1}/3)…")
                        await asyncio.sleep(1 * (2 ** attempt))
                if r is None:
                    await log("[LLM检索] responses 重试后仍失败")
                    return None
                if r.status_code >= 400:
                    try:
                        body = r.text
                    except Exception:
                        body = ""
                    await log(f"[LLM检索] responses 返回 {r.status_code}，body={body[:200]}，无法使用该端点…")
                    return None
                data = r.json()
                # Some responses APIs return a top-level 'output_text'
                content = data.get('output_text') or data.get('content') or ''
                arr = _extract_json_array(content)
                if not arr:
                    await log("[LLM检索] responses 未解析到JSON数组，忽略此端点结果…")
                    return None
                items: List[Dict] = []
                for it in arr:
                    if not isinstance(it, dict):
                        continue
                    url0 = it.get('url') or it.get('href') or ''
                    items.append({
                        'href': url0,
                        'title': it.get('title') or '',
                        'source': it.get('source') or (domain_of(url0) or ''),
                        'date': it.get('date') or '',
                        'summary': it.get('summary') or '',
                    })
                return self._rank_results(items)
        except Exception as e:
            await log(f"[LLM检索] responses 异常：{e}")
            return None

    async def search(self, query: str) -> List[Dict]:
        # 打印规范化后的 API 前缀，避免 /v1 误配引起的困惑
        await log(f"[LLM检索] 使用AiHubMix: base={self._base_api}, model={self.model_id}")
        # Prefer chat/completions (docs mention 'messages')
        items = await self._call_chat_completions(query)
        if items:
            await log(f"[LLM检索] chat/completions 命中 {len(items)} 条")
            return items
        # Fallback to responses
        items = await self._call_responses(query)
        if items:
            await log(f"[LLM检索] responses 命中 {len(items)} 条")
            return items
        await log("[LLM检索] 两个端点均未返回有效结果")
        return []

    async def fetch_readable(self, url: str, timeout: int = 15) -> Tuple[str, str]:
        """Return (title, text) extracted from article HTML; text may be empty on failure."""
        try:
            client_kwargs = { 'follow_redirects': True, 'timeout': timeout, 'verify': False, 'trust_env': False }
            proxies = self._proxies_if_reachable()
            if proxies:
                client_kwargs['proxies'] = proxies
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(url)
                ctype = (resp.headers.get('content-type') or '').lower()
                if 'text/html' not in ctype:
                    return '', ''
                html = resp.text
                doc = Document(html)
                title = doc.short_title()
                content_html = doc.summary(html_partial=True)
                soup = BeautifulSoup(content_html, 'lxml')
                text = soup.get_text('\n')
                return title or '', text or ''
        except Exception:
            return '', ''

    async def gather_readables(self, items: List[Dict]) -> List[Dict]:
        selected = items[:self.max_fetch_html]
        tasks = [self.fetch_readable(it.get('href')) for it in selected]
        results = await asyncio.gather(*tasks)
        merged = []
        for it, (title, text) in zip(selected, results):
            it2 = dict(it)
            it2['article_title'] = title
            it2['article_text'] = text
            merged.append(it2)
        return merged