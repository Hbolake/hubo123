import asyncio
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse


def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


class MockSearchService:
    """
    离线/演示用的检索服务：不访问网络，直接返回内置的样例数据，并携带 article_text。
    仅用于本地验证 UI 与两阶段报告生成流程。
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

    def _rank(self, items: List[Dict]) -> List[Dict]:
        trusted, others = [], []
        for it in items:
            dom = domain_of(it.get('href') or '')
            if dom in self.blacklist_domains:
                continue
            if self.trusted_domains and dom in self.trusted_domains:
                trusted.append(it)
            else:
                others.append(it)
        ranked = trusted + ([] if self.only_trusted else others)
        return ranked[: self.max_results]

    async def search(self, query: str) -> List[Dict]:
        samples: List[Dict] = [
            {
                'href': 'https://example.com/aifilm/industry_trends',
                'title': 'AI 电影产业趋势速览',
                'source': 'example.com',
                'summary': '概述近一年 AI 与电影行业的应用进展与典型事件',
                'date': '2025-06-01',
            },
            {
                'href': 'https://example.com/aifilm/opinion_landscape',
                'title': '舆情与争议点分布',
                'source': 'example.com',
                'summary': '整理创作者、平台、观众的不同观点与主要争议焦点',
                'date': '2025-05-20',
            },
            {
                'href': 'https://example.com/aifilm/regulation',
                'title': '政策与监管动态',
                'source': 'example.com',
                'summary': '归纳近半年政策与行业规范的更新要点',
                'date': '2025-04-10',
            },
        ]
        return self._rank(samples)

    async def fetch_readable(self, url: str) -> Tuple[str, str]:
        # 模拟正文，确保报告只基于“抓取到的正文”生成
        content_map = {
            'https://example.com/aifilm/industry_trends': (
                'AI 电影产业趋势速览',
                '过去一年，生成式 AI 在电影制作各环节的应用更为普及：剧本辅助、分镜与预可视化、特效合成与清洁等均出现效率提升案例。大型制片厂更重视合规与资产管理，小型团队偏向快速试验。资本层面回归理性，关注能落地的场景与成本结构优化。'
            ),
            'https://example.com/aifilm/opinion_landscape': (
                '舆情与争议点分布',
                '从创作者看，AI 被视为新工具但需避免风格同质化；从平台看，版权与模型来源透明是重点；从观众看，题材创新与真实质感是核心诉求。共识是：明确署名、数据来源合规、风险提示到位。'
            ),
            'https://example.com/aifilm/regulation': (
                '政策与监管动态',
                '近半年内，行业对训练数据合规、生成内容标识与侵权责任的讨论增多。部分地区出台指导意见，鼓励在可控范围内推进 AI 赋能影视生产，同时强调审查与内容安全要求。'
            ),
        }
        title, text = content_map.get(url, ('', ''))
        return title, text

    async def gather_readables(self, items: List[Dict]) -> List[Dict]:
        selected = items[: self.max_fetch_html]
        tasks = [self.fetch_readable(it.get('href')) for it in selected]
        results = await asyncio.gather(*tasks)
        merged = []
        for it, (title, text) in zip(selected, results):
            it2 = dict(it)
            it2['article_title'] = title
            it2['article_text'] = text
            merged.append(it2)
        return merged