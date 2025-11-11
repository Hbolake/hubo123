import os
import re
import asyncio
import json
import time
from typing import List, Dict, Optional

import httpx

from src.utils.logger import log
from src.adapters.llm_doubao import DoubaoClient


def _domain(url: str) -> str:
    try:
        import urllib.parse
        return (urllib.parse.urlparse(url).hostname or '').lower()
    except Exception:
        return ''


def _normalize_url(url: str, base: str) -> str:
    try:
        import urllib.parse
        return urllib.parse.urljoin(base, url)
    except Exception:
        return url


def _strip_html(html: str) -> str:
    if not html:
        return ''
    # remove scripts/styles
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
    # pick <p> blocks first; fallback to text
    ps = re.findall(r"<p[^>]*>([\s\S]*?)</p>", html, flags=re.IGNORECASE)
    text = "\n".join([re.sub(r"<[^>]+>", " ", p) for p in ps])
    if not text.strip():
        text = re.sub(r"<[^>]+>", " ", html)
    # normalize spaces
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class CrawlerService:
    """站内轻量抓取服务（PoC）
    - 优先从白名单站点的首页/频道页抓取与主题关键词匹配的链接
    - 排除视频站点/自媒体域名
    - 对每个链接抓取正文，输出 items 与 gather_readables 兼容格式
    """

    def __init__(self,
                 per_site_limit: int = 5,
                 max_total_limit: int = 30,
                 time_days: int = 15,
                 trusted_domains: Optional[List[str]] = None,
                 blacklist_domains: Optional[List[str]] = None):
        self.per_site_limit = per_site_limit
        self.max_total_limit = max_total_limit
        self.time_days = time_days
        self.trusted_domains = trusted_domains or []
        self.blacklist_domains = blacklist_domains or []
        self.http_proxy = os.getenv('HTTP_PROXY', '')
        # 自动抑制配置（对连续失败的站点进行临时跳过）
        self.suppress_file = os.getenv('SUPPRESS_FILE', '.suppress_domains.json')
        self.suppress_threshold = int(os.getenv('SUPPRESS_THRESHOLD', '3'))
        self.suppress_hours = int(os.getenv('SUPPRESS_TTL_HOURS', '24'))
        self._suppress: Dict[str, Dict] = self._load_suppress()

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

    async def _client(self) -> httpx.AsyncClient:
        kwargs = {
            'timeout': httpx.Timeout(40.0, connect=20.0, read=40.0, write=40.0),
            'verify': False,
            'trust_env': False,
            'http2': True,
        }
        proxies = self._proxies_if_reachable()
        if proxies:
            kwargs['proxies'] = proxies
            await log(f"[Crawler] 代理启用: {proxies}")
        else:
            await log("[Crawler] 直连请求")
        return httpx.AsyncClient(**kwargs)

    async def _generate_keywords(self, topic: str) -> List[str]:
        llm = DoubaoClient()
        messages = [
            {'role': 'system', 'content': '你是中文关键词专家。请仅返回JSON，不要解释。'},
            {'role': 'user', 'content': f'主题：{topic}\n请生成最多12个中文关键词（包含同义词/常用简称/相关概念），返回格式：{{"keywords": ["词1","词2",...] }}'}
        ]
        try:
            r = await llm.complete(messages)
            text = r.get('choices', [{}])[0].get('message', {}).get('content', '')
            # 解析 JSON
            import json, re
            t = text.strip()
            t = re.sub(r"^```(?:json)?\r?\n", "", t)
            t = re.sub(r"\r?\n```$", "", t)
            m = re.search(r"\{", t)
            n = t.rfind("}")
            if m and n != -1 and n > m.start():
                snippet = t[m.start():n+1]
                data = json.loads(snippet)
            else:
                data = json.loads(t)
            kws = [s.strip() for s in (data.get('keywords') or []) if isinstance(s, str) and s.strip()]
            return kws[:12]
        except Exception as e:
            await log(f"[Crawler] 关键词扩展失败：{e}，改用原始主题分词…")
            # 简单切词：按非中文字/空格分割
            parts = re.split(r"[^\u4e00-\u9fa5A-Za-z0-9]+", topic)
            return [p for p in parts if p][:8]

    async def search(self, topic: str) -> List[Dict]:
        # 生成关键词
        keywords = await self._generate_keywords(topic)
        await log(f"[Crawler] 关键词集：{keywords}")

        # 准备站点入口
        seeds: Dict[str, str] = {}
        for d in self.trusted_domains:
            scheme = 'https://'
            seeds[d] = f"{scheme}{d}/"
        
        tasks = []
        async with await self._client() as client:
            for domain, base_url in seeds.items():
                task = asyncio.wait_for(
                    self._search_one_domain(client, domain, base_url, keywords),
                    timeout=60.0  # 为每个域名的抓取设置60秒超时
                )
                tasks.append(task)
            
            results_list = await asyncio.gather(*tasks, return_exceptions=True)
        
        results: List[Dict] = []
        for res in results_list:
            if isinstance(res, list):
                results.extend(res)
            elif isinstance(res, Exception):
                await log(f"[Crawler] 域名抓取任务异常: {res}")

        return results[:self.max_total_limit]

    async def _search_one_domain(self, client: httpx.AsyncClient, domain: str, base_url: str, keywords: List[str]) -> List[Dict]:
        # 频道页补充入口（尽量覆盖汽车/科技/财经相关），域名别名（新华网 -> news.cn）
        extra_seeds: Dict[str, List[str]] = {
            'xinhuanet.com': [
                'https://www.xinhuanet.com/auto/',
                'https://www.xinhuanet.com/fortune/',
                'https://www.xinhuanet.com/tech/',
            ],
            'thepaper.cn': [
                'https://www.thepaper.cn/channel/26916',
                'https://www.thepaper.cn/channel/26835',
                'https://www.thepaper.cn/channel/27224',
            ],
            '163.com': [
                'https://auto.163.com/',
                'https://news.163.com/',
            ],
        }
        domain_aliases: Dict[str, List[str]] = {
            'xinhuanet.com': ['news.cn', 'www.news.cn'],
        }

        if domain in self.blacklist_domains:
            await log(f"[Crawler] 跳过黑名单：{domain}")
            return []
        # 检查自动抑制
        sup = self._suppress.get(domain)
        now = int(time.time())
        if sup and sup.get('suppress_until', 0) > now:
            hours_left = max(1, int((sup['suppress_until'] - now) / 3600))
            await log(f"[Crawler] 抑制域名：{domain}（连续失败{sup.get('count', 0)}次，剩余{hours_left}小时）")
            return []
        
        items: List[Dict] = []
        try:
            # 优先尝试站点 sitemap（更稳定），失败再回退首页/频道页
            sitemap_items = await self._collect_from_sitemap(client, base_url, keywords)
            items.extend(sitemap_items[:self.per_site_limit])

            if len(items) < self.per_site_limit:
                # 回退：直接从首页提取包含关键词的链接
                resp = await client.get(base_url, follow_redirects=True)
                if resp.status_code < 400:
                    html = resp.text
                    anchors = re.findall(r"<a[^>]+href=\"([^\"]+)\"[^>]*>([\s\S]*?)</a>", html, flags=re.IGNORECASE)
                    for href, text in anchors:
                        url = _normalize_url(href, base_url)
                        udom = _domain(url)
                        if not self._is_same_site(udom, domain, domain_aliases):
                            continue
                        if any(kw in (text or '') for kw in keywords) or any(kw in url for kw in keywords):
                            if any(x in url for x in ['video', '/v/', '/tv/', 'live']):
                                continue
                            items.append({
                                'href': url,
                                'title': re.sub(r"<[^>]+>", " ", text).strip()[:80],
                                'source': domain,
                            })
                        if len(items) >= self.per_site_limit:
                            break
                
                # 频道页补充抓取
                for seed in extra_seeds.get(domain, []):
                    if len(items) >= self.per_site_limit:
                        break
                    try:
                        r = await client.get(seed, follow_redirects=True)
                        if r.status_code >= 400:
                            continue
                        ah = re.findall(r"<a[^>]+href=\"([^\"]+)\"[^>]*>([\s\S]*?)</a>", r.text, flags=re.IGNORECASE)
                        for href, text in ah:
                            url = _normalize_url(href, seed)
                            udom = _domain(url)
                            if not self._is_same_site(udom, domain, domain_aliases):
                                continue
                            if any(kw in (text or '') for kw in keywords) or any(kw in url for kw in keywords):
                                if any(x in url for x in ['video', '/v/', '/tv/', 'live']):
                                    continue
                                items.append({
                                    'href': url,
                                    'title': re.sub(r"<[^>]+>", " ", text).strip()[:80],
                                    'source': domain,
                                })
                            if len(items) >= self.per_site_limit:
                                break
                    except Exception:
                        continue
            
            await log(f"[Crawler] {domain} 命中候选 {len(items)} 条（sitemap+首页回退）")
            # 若本次成功命中，则清理该域名的失败计数/抑制
            if len(items) > 0:
                self._record_success(domain)
            
            return items[:self.per_site_limit]
        except Exception as e:
            self._record_failure(domain)
            await log(f"[Crawler] 抓取入口失败 {domain}：{e}")
            raise

    async def gather_readables(self, items: List[Dict]) -> List[Dict]:
        # 对每个链接抓取正文
        readables: List[Dict] = []
        async with await self._client() as client:
            sem = asyncio.Semaphore(3)

            async def fetch_one(it: Dict):
                async with sem:
                    url = it.get('href') or ''
                    try:
                        resp = await client.get(url, follow_redirects=True)
                        if resp.status_code >= 400:
                            return {**it, 'article_text': ''}
                        text = _strip_html(resp.text)
                        # 轻量时间/标题提取
                        title_m = re.search(r"<title>([\s\S]*?)</title>", resp.text, flags=re.IGNORECASE)
                        if title_m:
                            it['article_title'] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", title_m.group(1))).strip()[:120]
                        # 日期简单抓取（可能不准确，但作为占位）
                        date_m = re.search(r"(20\d{2}[\-/年]\d{1,2}[\-/月]\d{1,2})", resp.text)
                        if date_m:
                            it['date'] = date_m.group(1)
                        return {**it, 'article_text': text}
                    except Exception:
                        return {**it, 'article_text': ''}

            tasks = [fetch_one(it) for it in items]
            for r in await asyncio.gather(*tasks):
                readables.append(r)
        return readables

    async def _collect_from_sitemap(self, client: httpx.AsyncClient, base_url: str, keywords: List[str]) -> List[Dict]:
        """尝试从常见 sitemap 端点收集包含关键词的近15天链接。"""
        endpoints = [
            'sitemap.xml', 'sitemap_index.xml', 'sitemapindex.xml',
            'sitemap-news.xml', 'sitemap-news.xml.gz',
        ]
        items: List[Dict] = []
        domain = _domain(base_url)
        for ep in endpoints:
            try:
                url = _normalize_url(ep, base_url)
                resp = await client.get(url, follow_redirects=True)
                if resp.status_code >= 400:
                    continue
                text = resp.text
                # 提取 <loc> 与可选 <lastmod>
                locs = re.findall(r"<loc>([^<]+)</loc>", text)
                lastmods = re.findall(r"<lastmod>([^<]+)</lastmod>", text)
                # 建立简单映射（可能长度不同）
                for idx, loc in enumerate(locs):
                    u = loc.strip()
                    if _domain(u) != domain:
                        continue
                    if any(kw in u for kw in keywords):
                        # 时间过滤（若可得）
                        ok_time = True
                        if idx < len(lastmods):
                            lm = lastmods[idx]
                            # 简单判断是否在 time_days 内（不严格）
                            # 允许通过：若无法解析则默认通过
                            try:
                                from datetime import datetime, timedelta
                                dt = datetime.fromisoformat(lm.replace('Z','').replace('T',' '))
                                if (datetime.utcnow() - dt) > timedelta(days=self.time_days):
                                    ok_time = False
                            except Exception:
                                ok_time = True
                        if ok_time:
                            items.append({'href': u, 'title': '', 'source': domain})
                    if len(items) >= self.per_site_limit:
                        break
            except Exception:
                continue
            if len(items) >= self.per_site_limit:
                break
        return items

    def _is_same_site(self, url_domain: str, base_domain: str, aliases: Dict[str, List[str]]) -> bool:
        if url_domain == base_domain:
            return True
        for k, vs in aliases.items():
            if base_domain == k and url_domain in vs:
                return True
        return False

    # --- 自动抑制实现 ---
    def _load_suppress(self) -> Dict[str, Dict]:
        try:
            if os.path.exists(self.suppress_file):
                with open(self.suppress_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
        except Exception:
            pass
        return {}

    def _save_suppress(self) -> None:
        try:
            with open(self.suppress_file, 'w', encoding='utf-8') as f:
                json.dump(self._suppress, f, ensure_ascii=False, indent=2)
        except Exception:
            # 异常忽略，避免影响主流程
            asyncio.create_task(log("[Crawler] 抑制列表保存失败（忽略）"))

    def _record_failure(self, domain: str) -> None:
        now = int(time.time())
        rec = self._suppress.get(domain) or {"count": 0, "suppress_until": 0}
        rec["count"] = int(rec.get("count", 0)) + 1
        if rec["count"] >= self.suppress_threshold:
            rec["suppress_until"] = now + self.suppress_hours * 3600
        self._suppress[domain] = rec
        self._save_suppress()

    def _record_success(self, domain: str) -> None:
        if domain in self._suppress:
            self._suppress.pop(domain, None)
            self._save_suppress()