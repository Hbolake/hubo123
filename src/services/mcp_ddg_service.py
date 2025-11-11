import asyncio
import re
import os
import shutil
from typing import List, Dict, Optional
from urllib.parse import urlparse

from src.utils.logger import log
from src.services.search_service import SearchService


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


class MCPDDGService:
    """
    基于 MCP (Model Context Protocol) 的 DuckDuckGo 搜索服务。
    优先通过本地子进程启动 duckduckgo-mcp-server 并调用其 tools：search / fetch_content。
    若 MCP 不可用或调用失败，将返回空列表，供上层按策略进行回退。

    约定输出：每项至少包含 href, title, source；随后复用 readability 抓取正文。
    """

    def __init__(self, max_results: int = 12, max_fetch_html: int = 8,
                 trusted_domains: Optional[List[str]] = None,
                 only_trusted: bool = False,
                 blacklist_domains: Optional[List[str]] = None):
        self.max_results = max_results
        self.max_fetch_html = max_fetch_html
        self.only_trusted = only_trusted
        self.trusted_domains = [urlparse(d).netloc or d for d in (trusted_domains or [])]
        self.blacklist_domains = [urlparse(d).netloc or d for d in (blacklist_domains or [])]
        # 用现有的 SearchService 抓取正文
        self._fetcher = SearchService(max_results=max_results, max_fetch_html=max_fetch_html,
                                      trusted_domains=trusted_domains, only_trusted=only_trusted,
                                      blacklist_domains=blacklist_domains)

    async def _rank(self, items: List[Dict]) -> List[Dict]:
        trusted, others = [], []
        for it in items:
            dom = _domain(it.get('href') or '')
            if dom in self.blacklist_domains:
                continue
            if self.trusted_domains and dom in self.trusted_domains:
                trusted.append(it)
            else:
                others.append(it)
        ranked = trusted + ([] if self.only_trusted else others)
        return ranked[: self.max_results]

    async def search(self, query: str) -> List[Dict]:
        try:
            from mcp.client.stdio import stdio_client, StdioServerParameters
        except Exception as e:
            await log(f"[MCP] mcp 客户端不可用（未安装或环境异常）：{e}")
            return []

        # 通过 stdio 启动 duckduckgo-mcp-server 子进程
        try:
            # 选择可执行文件：优先环境变量 DDG_MCP_SERVER；其次 PATH 中的 duckduckgo-mcp-server；
            # 再次尝试当前虚拟环境下的 bin 目录。
            exe_candidates = []
            env_exe = os.getenv("DDG_MCP_SERVER")
            if env_exe:
                exe_candidates.append(env_exe)
            exe_candidates.append("duckduckgo-mcp-server")
            venv = os.getenv("VIRTUAL_ENV")
            if venv:
                exe_candidates.append(os.path.join(venv, "bin", "duckduckgo-mcp-server"))
            # 选择第一个存在的可执行路径
            exe_resolved = None
            for cand in exe_candidates:
                if shutil.which(cand) or os.path.exists(cand):
                    exe_resolved = cand
                    break
            if not exe_resolved:
                await log(f"[MCP] 未找到 duckduckgo-mcp-server 可执行文件，候选={exe_candidates}")
                return []

            # 传递代理环境变量给子进程（如需）并捕获服务端标准错误日志
            env = os.environ.copy()
            http_proxy = os.getenv('HTTP_PROXY') or ''
            if http_proxy:
                env['HTTP_PROXY'] = http_proxy
                env['HTTPS_PROXY'] = http_proxy
            params = StdioServerParameters(command=exe_resolved, args=[], env=env)
            # 将服务端错误输出重定向到本地文件，便于诊断 TaskGroup 异常的根因
            errlog_path = os.path.join(os.getcwd(), 'mcp_ddg_err.log')
            with open(errlog_path, 'w', encoding='utf-8') as errfp:
                async with stdio_client(params, errlog=errfp) as client:
                    # 列出工具
                    tools = await client.list_tools()
                    names = [t.name for t in tools]
                    await log(f"[MCP] 可用工具: {names}")
                    if "search" not in names:
                        await log("[MCP] 未发现 search 工具")
                        return []
                # 调用 search
                res = await client.call_tool("search", {"query": query, "max_results": self.max_results})
                # MCP 结果可能是富文本或结构体，尽量解析 URL / 标题
                items: List[Dict] = []
                # 支持 result.output.text 形式
                text = ""
                try:
                    text = (res.output or {}).get("text") or ""
                except Exception:
                    pass
                if isinstance(text, str) and text:
                    urls = re.findall(r"https?://[\w\-\.\?\=/#%&:+]+", text)
                    titles = re.findall(r"^\s*\d+\.\s*(.+)$", text, flags=re.M)
                    for u in dict.fromkeys(urls):
                        items.append({
                            'href': u,
                            'title': '',
                            'source': _domain(u) or '',
                        })
                # 支持结构化 items
                try:
                    for o in (res.output or {}).get("items", []):
                        u = o.get("url") or o.get("href") or ""
                        if not u:
                            continue
                        items.append({
                            'href': u,
                            'title': o.get("title") or '',
                            'source': o.get("source") or (_domain(u) or ''),
                        })
                except Exception:
                    pass
                if not items:
                    await log("[MCP] DuckDuckGo 未返回可解析结果")
                    return []
                ranked = await self._rank(items)
                return ranked
        except Exception as e:
            await log(f"[MCP] 运行 duckduckgo-mcp-server 失败：{e}")
            return []

    async def fetch_readable(self, item: Dict) -> Dict:
        # 直接复用 SearchService 的抓取逻辑
        return await self._fetcher.fetch_readable(item)

    async def gather_readables(self, items: List[Dict]) -> List[Dict]:
        return await self._fetcher.gather_readables(items)