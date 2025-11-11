import asyncio
from typing import List, Dict, Tuple, Optional
from urllib.parse import urlparse

from duckduckgo_search import DDGS
import httpx
from bs4 import BeautifulSoup
from readability import Document


def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


class SearchService:
    def __init__(self, max_results: int = 12, max_fetch_html: int = 8,
                 trusted_domains: Optional[List[str]] = None,
                 only_trusted: bool = False,
                 blacklist_domains: Optional[List[str]] = None):
        self.max_results = max_results
        self.max_fetch_html = max_fetch_html
        self.only_trusted = only_trusted
        self.trusted_domains = [urlparse(d).netloc or d for d in (trusted_domains or [])]
        self.blacklist_domains = [urlparse(d).netloc or d for d in (blacklist_domains or [])]

    def _rank_results(self, items: List[Dict]) -> List[Dict]:
        trusted, others = [], []
        for it in items:
            dom = domain_of(it.get('href') or it.get('link') or '')
            if dom in self.blacklist_domains:
                continue
            if self.trusted_domains and dom in self.trusted_domains:
                trusted.append(it)
            else:
                others.append(it)
        ranked = trusted + ([] if self.only_trusted else others)
        return ranked[:self.max_results]

    async def search(self, query: str) -> List[Dict]:
        ddgs = DDGS()
        results = list(ddgs.text(query, max_results=self.max_results * 2))
        # normalize keys
        for r in results:
            if 'href' not in r and 'link' in r:
                r['href'] = r['link']
        return self._rank_results(results)

    async def fetch_readable(self, url: str, timeout: int = 120) -> Tuple[str, str]:
        """Return (title, text) extracted from article HTML; text may be empty on failure.
        增强稳定性：connect=30s, read/write=120s，最多重试3次，指数退避。
        """
        try:
            client_kwargs = {
                'follow_redirects': True,
                'timeout': httpx.Timeout(timeout, connect=30.0, read=timeout, write=timeout),
                'http2': True,
            }
            async with httpx.AsyncClient(**client_kwargs) as client:
                last_exc = None
                for attempt in range(3):
                    try:
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
                    except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.HTTPError) as e:
                        last_exc = e
                        await asyncio.sleep(1 * (2 ** attempt))
                    except Exception as e:
                        last_exc = e
                        break
                return '', ''
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