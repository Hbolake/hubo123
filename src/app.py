import os
import asyncio
from typing import List, Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from src.utils.logger import log_bus, log
# reload trigger: update config for provider/whitelist/blacklist
from src.services.search_service import SearchService
from src.services.mock_service import MockSearchService
from src.services.llm_search_service import LLMSearchService
from src.services.mcp_ddg_service import MCPDDGService
from src.adapters.llm_doubao import DoubaoClient
from src.services.report_service import (
    build_prompt,
    build_fallback_markdown,
    build_expert_structure_messages,
    build_expert_markdown_messages,
    try_parse_json_object,
)
from src.services.pdf_service import md_to_html_body, build_full_html, html_to_pdf
from src.services.crawler_service import CrawlerService


load_dotenv()

app = FastAPI(title="微舆·简版")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(log_bus.broadcaster())


@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    await ws.accept()
    q = await log_bus.subscribe()
    try:
        await log("[系统] 日志流已连接")
        while True:
            msg = await q.get()
            await ws.send_text(msg)
    except WebSocketDisconnect:
        log_bus.unsubscribe(q)


@app.post("/analyze")
async def analyze(payload: Dict):
    topic = payload.get("topic") or ""
    if not topic:
        return JSONResponse({"error": "缺少主题"}, status_code=400)

    # load config
    max_results = int(os.getenv("MAX_RESULTS", "12"))
    max_fetch = int(os.getenv("MAX_FETCH_HTML", "8"))
    only_trusted = os.getenv("ONLY_TRUSTED", "false").lower() == "true"
    trusted_csv = os.getenv("TRUSTED_DOMAINS", "")
    blacklist_csv = os.getenv("BLACKLIST_DOMAINS", "")
    report_dir = os.getenv("REPORT_DIR", "reports")

    trusted = [d.strip() for d in trusted_csv.split(',') if d.strip()]
    blacklist = [d.strip() for d in blacklist_csv.split(',') if d.strip()]

    provider = os.getenv("SEARCH_PROVIDER", "ddgs").lower()
    if provider == "llm":
        # LLM检索（AiHubMix）配置
        llm_base = os.getenv("LLM_SEARCH_BASE_URL", "https://aihubmix.com")
        # 使用实际可访问的默认模型，避免 403 权限错误
        llm_model = os.getenv("LLM_SEARCH_MODEL_ID", "gemini-2.5-pro")
        llm_key = os.getenv("LLM_SEARCH_API_KEY", "")
        time_days = int(os.getenv("LLM_SEARCH_TIME_DAYS", os.getenv("TIME_RANGE_DAYS", "30")))
        language = os.getenv("LLM_SEARCH_LANGUAGE", os.getenv("LANGUAGE", "zh"))
        ss = LLMSearchService(max_results=max_results, max_fetch_html=max_fetch,
                              trusted_domains=trusted, only_trusted=only_trusted,
                              blacklist_domains=blacklist,
                              base_url=llm_base, api_key=llm_key, model_id=llm_model,
                              language=language, time_days=time_days)
        # 为避免 500，若 LLM 未返回结果，允许临时回退到 DDGS 文本搜索
        ddgs_fallback = SearchService(max_results=max_results, max_fetch_html=max_fetch,
                                      trusted_domains=trusted, only_trusted=only_trusted,
                                      blacklist_domains=blacklist)
        await log(f"[配置] 使用 LLM 联网检索服务: base={llm_base}, model={llm_model}")
    elif provider == "mcp":
        ss = MCPDDGService(max_results=max_results, max_fetch_html=max_fetch,
                           trusted_domains=trusted, only_trusted=only_trusted,
                           blacklist_domains=blacklist)
        # AiHubMix 作为补充/回退
        llm_base = os.getenv("LLM_SEARCH_BASE_URL", "https://aihubmix.com")
        llm_model = os.getenv("LLM_SEARCH_MODEL_ID", "gemini-2.5-pro")
        llm_key = os.getenv("LLM_SEARCH_API_KEY", "")
        time_days = int(os.getenv("LLM_SEARCH_TIME_DAYS", os.getenv("TIME_RANGE_DAYS", "30")))
        language = os.getenv("LLM_SEARCH_LANGUAGE", os.getenv("LANGUAGE", "zh"))
        ddgs_fallback = LLMSearchService(max_results=max_results, max_fetch_html=max_fetch,
                                         trusted_domains=trusted, only_trusted=only_trusted,
                                         blacklist_domains=blacklist,
                                         base_url=llm_base, api_key=llm_key, model_id=llm_model,
                                         language=language, time_days=time_days)
        await log("[配置] 使用 MCP DuckDuckGo 搜索；AiHubMix 作为回退")
    elif provider == "mock":
        ss = MockSearchService(max_results=max_results, max_fetch_html=max_fetch,
                               trusted_domains=trusted, only_trusted=only_trusted,
                               blacklist_domains=blacklist)
        ddgs_fallback = None
        await log("[配置] 使用本地离线 Mock 检索服务（演示/断网验证）")
    elif provider == "crawler":
        # 站内抓取（PoC）：按白名单站点进行首页/频道页轻量抓取
        per_site = int(os.getenv("PER_SITE_LIMIT", "5"))
        max_total = int(os.getenv("MAX_TOTAL_LIMIT", "30"))
        time_days = int(os.getenv("TIME_RANGE_DAYS", "15"))
        # 是否严格禁用任何非爬虫的搜索回退（只走爬虫抓取）
        crawler_strict = os.getenv("CRAWLER_STRICT", os.getenv("DISABLE_SEARCH_FALLBACK", "false")).lower() == "true"
        if not trusted:
            trusted = [
                'people.com.cn','xinhuanet.com','cctv.com','thepaper.cn',
                'bjnews.com.cn','ifeng.com','163.com','sohu.com'
            ]
        if not blacklist:
            blacklist = [
                'weixin.qq.com','mp.weixin.qq.com','baijiahao.baidu.com',
                'bilibili.com','iqiyi.com','youku.com','bilibili.tv','acfun.cn'
            ]
        ss = CrawlerService(per_site_limit=per_site, max_total_limit=max_total,
                            time_days=time_days, trusted_domains=trusted,
                            blacklist_domains=blacklist)
        # 按配置控制是否允许搜索回退（严格模式下不回退到 DDGS/LLM）
        if crawler_strict:
            ddgs_fallback = None
            await log("[配置] 使用站内抓取（Crawler）模式（严格）：禁用任何非爬虫的搜索回退")
        else:
            # 当 Crawler 未命中任何候选时，自动回退到 DDGS 文本检索，避免直接 502
            ddgs_fallback = SearchService(max_results=max_results, max_fetch_html=max_fetch,
                                          trusted_domains=trusted, only_trusted=only_trusted,
                                          blacklist_domains=blacklist)
            await log("[配置] 使用站内抓取（Crawler）模式：白名单站点轻量抓取与正文抽取；允许 DDGS 作为兜底")
    else:
        ss = SearchService(max_results=max_results, max_fetch_html=max_fetch,
                           trusted_domains=trusted, only_trusted=only_trusted,
                           blacklist_domains=blacklist)
        ddgs_fallback = None

    await log(f"[搜索] 开始：{topic}")
    try:
        items = await ss.search(topic)
        # Crawler 为空时，自动回退到 DDGS
        if provider == "crawler" and len(items) == 0 and ddgs_fallback is not None:
            await log("[搜索] Crawler 未返回结果，回退至 DDGS 文本检索…")
            items = await ddgs_fallback.search(topic)
            ss = ddgs_fallback
        # MCP 为空时，回退到 AiHubMix（若已配置）
        if provider == "mcp" and len(items) == 0 and ddgs_fallback is not None:
            await log("[搜索] MCP 未返回结果，回退至 AiHubMix LLM 联网检索…")
            items = await ddgs_fallback.search(topic)
            ss = ddgs_fallback
        # LLM 为空时，临时回退至 DDGS，避免直接返回 500
        if provider == "llm" and len(items) == 0 and ddgs_fallback is not None:
            await log("[搜索] LLM 未返回结果，回退至 DDGS 文本检索…")
            items = await ddgs_fallback.search(topic)
            ss = ddgs_fallback
    except Exception as e:
        await log(f"[搜索] 异常：{e}，将继续流程并返回空结果提示…")
        items = []

    # 若仍为空，则不再直接返回 502，改为继续生成草稿报告并附带提示
    search_empty = (len(items) == 0)
    if search_empty:
        await log("[搜索] 未获取到任何结果，将生成草稿报告以兜底…")
    else:
        await log(f"[搜索] 命中 {len(items)} 条，优先白名单 {len(trusted)} 个域名")

    try:
        items_readable = await ss.gather_readables(items)
        got_text = sum(1 for it in items_readable if (it.get('article_text') or '').strip())
        total = len(items_readable)
        await log(f"[抓取] 已提取正文 {got_text}/{total}")
        # 不再在此处返回 502；当检索/抓取为空时，继续走草稿报告兜底
        if got_text == 0:
            await log("[抓取] 正文为空，继续使用检索摘要生成草稿报告…")
            # 若正文为空，关闭专家模式，直接走经典单阶段或草稿兜底
            os.environ['EXPERT_MODE'] = 'false'
    except Exception as e:
        await log(f"[抓取] 失败：{e}")
        return JSONResponse({"error": "抓取正文失败", "detail": str(e)}, status_code=500)

    expert_mode = os.getenv("EXPERT_MODE", "true").lower() == "true"
    llm = DoubaoClient()
    content = ""
    if expert_mode:
        await log("[专家模式] 阶段1：请求结构化 JSON…")
        try:
            msgs1 = build_expert_structure_messages(topic, items_readable)
            r1 = await llm.complete(msgs1)
            json_text = r1.get('choices', [{}])[0].get('message', {}).get('content', '')
            data = try_parse_json_object(json_text)
            if not data or not isinstance(data, dict) or not data.get('references'):
                await log("[专家模式] 结构化输出解析失败，切回经典单阶段提示…")
                expert_mode = False
            else:
                await log("[专家模式] 阶段2：根据结构化数据生成Markdown…")
                msgs2 = build_expert_markdown_messages(topic, json_text)
                r2 = await llm.complete(msgs2)
                content = r2.get('choices', [{}])[0].get('message', {}).get('content', '')
        except Exception as e:
            await log(f"[专家模式] LLM 调用失败：{e}，使用草稿报告兜底…")
            content = build_fallback_markdown(topic, items_readable)
    if not expert_mode:
        # 若无任何已抓取的正文证据，直接使用草稿报告兜底，避免模型返回“未提供证据”的错误提示
        has_evidence = any([(it.get('article_text') or '').strip() for it in items_readable])
        if not has_evidence:
            await log("[LLM] 无可用正文证据，使用草稿报告兜底…")
            content = build_fallback_markdown(topic, items_readable)
        else:
            messages = build_prompt(topic, items_readable)
            await log("[LLM] 请求豆包模型生成报告…")
            try:
                result = await llm.complete(messages)
                content = result.get('choices', [{}])[0].get('message', {}).get('content', '')
            except Exception as e:
                await log(f"[LLM] 失败：{e}，将使用检索结果生成草稿报告以兜底…")
                content = build_fallback_markdown(topic, items_readable)
        # 如果模型仍返回“未提供已抓取正文证据”的提示，二次兜底为草稿报告
        if not content or ('未提供“已抓取正文”的证据列表' in content or '未提供证据' in content):
            await log("[LLM] 模型返回内容不足，改用草稿报告兜底…")
            content = build_fallback_markdown(topic, items_readable)

    await log("[报告] 已生成Markdown，开始构建HTML与导出PDF…")

    os.makedirs(report_dir, exist_ok=True)
    # save md
    file_id = str(int(asyncio.get_event_loop().time()*1000))
    md_path = os.path.join(report_dir, f"report_{file_id}.md")
    pdf_path = os.path.join(report_dir, f"report_{file_id}.pdf")
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(content)
    try:
        body_html = md_to_html_body(content)
        full_html = build_full_html(body_html, title=topic)
        html_to_pdf(full_html, pdf_path)
        await log(f"[PDF] 导出成功：{pdf_path}")
    except Exception as e:
        await log(f"[PDF] 导出失败：{e}")

    # 低抓取率提示（不注入到报告正文，只作为前端提示）
    notice = ""
    try:
        rate = (got_text / total) if total else 0.0
        threshold = float(os.getenv("LOW_FETCH_RATE_THRESHOLD", "0.4"))
        min_docs = int(os.getenv("LOW_FETCH_MIN_DOCS", "3"))
        if search_empty:
            notice = "当前检索为空，已生成草稿报告；建议调整主题或稍后重试。"
        elif got_text < min_docs or rate < threshold:
            notice = f"当前抓取成功 {got_text}/{total}（{int(rate*100)}%），内容不足，建议调整主题或稍后重新运行。"
    except Exception:
        pass

    return {
        "markdown_path": md_path,
        "pdf_path": pdf_path,
        "markdown": content,
        "html": body_html,
        "mode": "expert" if os.getenv("EXPERT_MODE", "true").lower() == "true" else "classic",
        "fallback": (
            '检索为空，已返回草稿报告' if search_empty else (
                'LLM 调用失败，已返回草稿报告' if '说明：由于模型服务暂时不可用' in content else ''
            )
        ),
        "notice": notice,
    }


@app.get("/")
async def home():
    # serve the SPA
    return FileResponse("static/index.html")


@app.get("/download/pdf")
async def download_pdf(path: str):
    return FileResponse(path, media_type='application/pdf', filename=os.path.basename(path))