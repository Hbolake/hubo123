from typing import List, Dict


def build_prompt(topic: str, items: List[Dict]) -> list[Dict[str, str]]:
    """Construct messages for LLM with concise evidence list.
    调整：仅基于成功抓取的正文(article_text)作为证据，移除任何解释性/检索摘要内容。
    """
    evidence_lines = []
    for it in items:
        # 仅保留成功提取到正文的条目
        article_text = (it.get('article_text') or '').strip()
        if not article_text:
            continue
        src = it.get('article_title') or it.get('title') or it.get('source') or ''
        url = it.get('href') or ''
        snippet = article_text[:800]
        evidence_lines.append(f"- 来源: {src}\n  链接: {url}\n  摘要: {snippet}")

    system = {
        'role': 'system',
        'content': '你是一名资深舆情分析师。请以简洁、结构化的Markdown输出分析报告，避免虚构，保持客观。所有判断仅依据已抓取到的正文内容，不得使用检索摘要或未验证线索。'
    }
    evidence_text = "\n\n".join(evidence_lines)
    user = {
        'role': 'user',
        'content': (
            f"主题: {topic}\n\n"
            "请严格基于以下“已抓取正文”的证据列表生成结构化的Markdown报告，包含：\n"
            "1) 舆情概览 2) 关键信息点 3) 风险与争议 4) 结论与建议 5) 参考来源列表。\n"
            "报告用中文，条理清晰，适合直接导出为PDF。\n\n"
            f"证据列表:\n{evidence_text}\n"
        )
    }
    return [system, user]


def build_fallback_markdown(topic: str, items: List[Dict]) -> str:
    """当 LLM 不可用时生成正式风格的 Markdown 报告（去除解释性文本）。
    仅基于已成功抓取的正文(article_text)进行内容组织；若正文不足，将尽量输出精简的“参考来源”。
    """
    topic = topic or "报告"
    lines: List[str] = []
    lines.append(f"# {topic}")
    lines.append("")
    # 执行摘要（根据已抓取正文的要点提炼，最多5条）
    lines.append("## 执行摘要")
    domains = {}
    for it in items:
        href = it.get('href') or it.get('url') or ''
        src = it.get('source') or ''
        if src:
            domains[src] = domains.get(src, 0) + 1
    if domains:
        dom_sorted = sorted(domains.items(), key=lambda x: (-x[1], x[0]))
        dom_text = ", ".join([f"{d}×{n}" for d, n in dom_sorted[:8]])
    else:
        dom_text = "无可用来源"
    lines.append(f"- 线索覆盖：命中 {len(items)} 条；来源分布：{dom_text}。")
    lines.append("")

    # 舆情概览（根据正文归纳）
    lines.append("## 舆情概览")
    lines.append(f"- 命中条数：{len(items)}")
    lines.append(f"- 来源分布：{dom_text}")
    lines.append("")

    # 关键议题分析（仅基于 article_text 摘取要点）
    lines.append("## 关键议题分析")
    for idx, it in enumerate(items, start=1):
        article_text = (it.get('article_text') or '').strip()
        if not article_text:
            continue
        title = it.get('article_title') or it.get('title') or it.get('source') or '未命名'
        href = it.get('href') or it.get('url') or ''
        src = it.get('source') or ''
        snippet = article_text
        snippet = (snippet or '').strip().replace('\n', ' ')
        if len(snippet) > 180:
            snippet = snippet[:180] + '…'
        bullet = f"- [{title}]({href}) — {src}"
        lines.append(bullet)
        if snippet:
            lines.append(f"  \n  摘要（正文摘录）：{snippet}")
    lines.append("")

    # 风险与影响评估（简版模板）
    lines.append("## 风险与影响评估")
    lines.append("- 品牌：结合正文线索评估正负面曝光与情感倾向（待补充）。")
    lines.append("- 政策监管/法律合规：关注监管动向与合规要求（待补充）。")
    lines.append("- 安全与公众感知：识别潜在安全事件与公众敏感点（待补充）。")
    lines.append("- 财务：关注商业化进度、投入产出与成本压力（待补充）。")
    lines.append("- 舆论引爆可能性：监测社交平台传播态势与关键节点（待补充）。")
    lines.append("")

    # 利益相关方与立场（简版模板）
    lines.append("## 利益相关方与立场")
    lines.append("- 媒体/KOL/机构/受众的态度与动机将随证据集完善补充。")
    lines.append("")

    # Conclusion
    lines.append("## 结论与建议")
    lines.append("- 根据当前已抓取的正文线索，建议持续跟踪并补充证据；在模型服务可用时生成完整专家版报告。")
    lines.append("")

    # References
    lines.append("## 参考来源")
    for it in items:
        title = it.get('title') or it.get('article_title') or (it.get('source') or '来源')
        href = it.get('href') or it.get('url') or ''
        src = it.get('source') or ''
        lines.append(f"- [{title}]({href}) — {src}")
    lines.append("")

    return "\n".join(lines)


# ===== Expert mode (two-stage) prompts =====
def _evidence_block(items: List[Dict]) -> str:
    """仅列出已抓取正文的证据块，避免模型基于检索摘要进行判断。"""
    lines: List[str] = []
    idx = 0
    for it in items:
        article_text = (it.get('article_text') or '').strip()
        if not article_text:
            continue
        idx += 1
        title = it.get('article_title') or it.get('title') or (it.get('source') or '来源')
        href = it.get('href') or it.get('url') or ''
        src = it.get('source') or ''
        date = it.get('date') or ''
        summary = article_text
        if summary:
            summary = summary.replace('\n', ' ')
            if len(summary) > 300:
                summary = summary[:300] + '…'
        lines.append(f"[{idx}] 标题：{title}\n来源：{src}\n链接：{href}\n日期：{date}\n摘要（正文摘录）：{summary}\n")
    return "\n".join(lines)


def build_expert_structure_messages(topic: str, items: List[Dict]) -> list[Dict[str, str]]:
    """Stage-1: Ask the model to return a strict JSON object describing topics/risks/stakeholders/recommendations/references."""
    system = {
        'role': 'system',
        'content': (
            "你是资深舆情分析师与风险顾问。所有关键判断必须由可验证来源支撑；仅依据已抓取到的正文进行判断；对不确定内容以审慎语气表达；明确争议点与信息边界；建议需具备行动、理由、风险、优先级。"
        )
    }
    user = {
        'role': 'user',
        'content': (
            f"主题：{topic}\n\n请基于以下证据列表，输出严格 JSON（不包含任何解释文本）。\n"
            "JSON 结构要求：\n"
            "- summary: [{ text }]\n"
            "- topics: [{ id, name, claim, evidence_ids, counter_evidence_ids, impact, confidence, confidence_reason }]\n"
            "- risks: [{ name, level, reason, trigger, mitigation, dimensions }]（维度需覆盖：品牌、政策监管、法律合规、安全、公众感知、财务、舆论引爆可能性）\n"
            "- stakeholders: [{ name, type, stance, motivation, evidence_ids }]\n"
            "- recommendations: [{ action, reason, risks, priority, horizon }]（优先级用 P0/P1/P2）\n"
            "- references: [{ id, title, source, url, date }]\n\n"
            "要求：\n"
            "1) 每个 claim 必须由 evidence_ids 支撑，证据仅来自已抓取的正文；存在冲突信息时填写 counter_evidence_ids，并在 confidence_reason 解释不确定性。\n"
            "2) confidence 使用 高/中/低，并写明理由（来源类型、一致性、时间新鲜度、是否有反证）。\n"
            "3) 仅返回 JSON，不要附加解释。\n\n"
            f"证据列表：\n{_evidence_block(items)}"
        )
    }
    return [system, user]


def build_expert_markdown_messages(topic: str, json_text: str) -> list[Dict[str, str]]:
    """Stage-2: Turn structured JSON into a unified Markdown report under 3500 words with [n] citations."""
    system = {
        'role': 'system',
        'content': (
            "你是资深舆情分析师与风险顾问。风格为学术审慎；避免绝对化；明确争议与边界；所有关键判断必须在句末用 [n] 引用（n 对应参考来源）。不得加入对用户的解释性文字。"
        )
    }
    user = {
        'role': 'user',
        'content': (
            f"请将下面的结构化 JSON（主题：{topic}）转写为中文 Markdown 专家报告，统一模板如下：\n"
            "1) 执行摘要（3–5条要点；包含2–3条建议并标注优先级）\n"
            "2) 舆情概览（时间线与关键节点；来源与传播路径；话题簇简表）\n"
            "3) 关键议题分析（每簇：主张→证据→反证/争议→影响→置信度【中等且给出理由】）\n"
            "   影响维度需覆盖：品牌、政策监管、法律合规、安全、公众感知、财务\n"
            "4) 风险与影响评估（风险清单：等级、理由、触发条件、缓释措施；包含“舆论引爆可能性”维度；给出简要风险矩阵）\n"
            "5) 利益相关方与立场（媒体/机构/KOL/受众的态度、动机与诉求）\n"
            "6) 结论与建议（短期与中长期；行动、理由、风险、优先级）\n"
            "7) 参考来源（[n] 标题 — 来源 — 链接 — 日期；正文中每个议题仅展示2–3个代表性引用，其余在参考来源保留）\n\n"
            "约束：全文不超过3500字；文中所有关键判断必须用 [n] 引用；每个议题末尾注明“置信度：中等（理由…）”。\n\n"
            f"结构化 JSON：\n```json\n{json_text}\n```"
        )
    }
    return [system, user]


def try_parse_json_object(text: str) -> Dict:
    """Try to parse a JSON object from model output. Supports fenced blocks. Returns {} on failure."""
    import json, re
    if not text:
        return {}
    t = text.strip()
    t = re.sub(r"^```(?:json)?\r?\n", "", t)
    t = re.sub(r"\r?\n```$", "", t)
    # find first '{' and last '}'
    m = re.search(r"\{", t)
    n = t.rfind("}")
    if m and n != -1 and n > m.start():
        snippet = t[m.start():n+1]
        try:
            data = json.loads(snippet)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    try:
        data = json.loads(t)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}