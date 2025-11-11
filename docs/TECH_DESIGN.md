# 简化版舆情分析工具 · 技术设计文档

## 1. 技术目标
- **快速原型**：优先保证核心链路跑通，技术选型以“快”和“简”为主。
- **异步优先**：I/O 密集型任务（网络请求、文件读写）全部采用异步，提升并发性能。
- **模块化**：各组件（搜索、抓取、LLM、日志）职责清晰，便于独立测试和替换。
- **流式处理**：从数据获取到报告生成，尽可能采用流式处理，降低延迟，提升用户体验。

## 2. 技术选型
- **后端框架**: `FastAPI` - 异步、高效，自带 OpenAPI 文档，适合快速开发 API。
- **HTTP 客户端**: `httpx` - 支持异步请求，与 FastAPI 完美集成。
- **LLM SDK**: `python-dotenv`, `requests` - 直接调用 API，保持轻量。
- **PDF 导出**: `WeasyPrint` - 比 `wkhtmltopdf` 更易于集成，通过 CSS 控制样式。
- **前端**: 原生 `HTML/CSS/JavaScript` - 无需构建，直接提供静态文件服务。
- **WebSocket**: `FastAPI` 内置支持，用于实时日志推送。

## 3. 项目结构
```
/
├── .venv/                  # 虚拟环境
├── docs/
│   ├── PRD.md              # 产品需求文档
│   └── TECH_DESIGN.md      # 技术设计文档
├── reports/                # 生成的报告（.md 和 .pdf）
├── src/
│   ├── __init__.py
│   ├── app.py              # FastAPI 应用主入口
│   ├── services/           # 核心服务
│   │   ├── __init__.py
│   │   ├── llm_service.py    # LLM 调用服务
│   │   ├── log_service.py    # 日志服务
│   │   └── mcp_ddg_service.py # 搜索与抓取服务
│   └── utils/
│       ├── __init__.py
│       └── report_util.py    # 报告生成与 PDF 导出
├── static/                 # 前端静态文件
│   ├── index.html
│   └── styles.css
├── .env                    # 环境变量
├── .gitignore
└── requirements.txt        # Python 依赖
```

## 4. 环境变量 (`.env`)
```
# .env
SEARCH_PROVIDER="ddg" # ddg, bing, serpapi
MAX_RESULTS=12
FETCH_HTML=true

DOUBAO_API_KEY="your_doubao_api_key"
DOUBAO_MODEL="your_doubao_model_id"
DOUBAO_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"

STREAM_OUTPUT=true
```

## 5. 核心流程
1. **前端触发**: 用户点击“开始生成”，通过 WebSocket 发送 `start_analysis` 消息，包含主题 `topic`。
2. **`app.py` 接收**: WebSocket 端点接收消息，启动 `main_flow` 协程。
3. **`mcp_ddg_service.py` - 搜索**: 调用 `search_with_ddg`，使用 `httpx` 异步请求 DuckDuckGo，获取搜索结果。
4. **`mcp_ddg_service.py` - 并行抓取**: 使用 `asyncio.gather` 并发抓取多个页面的正文，设置超时 (`timeout=5`)，有效提升了性能和稳定性。
5. **`llm_service.py` - 生成报告**: 调用 `generate_report_stream`，将搜索结果和抓取内容组装成 Prompt，通过 `httpx` 流式请求豆包 LLM API。
6. **`log_service.py` - 实时日志**: 在流程的每个关键节点（开始、搜索成功、抓取完成、报告生成中），调用 `log_service.info()`，通过 WebSocket 将日志实时推送到前端。
7. **`report_util.py` - 保存与导出**: 报告生成完毕后，保存为 Markdown 文件，并调用 `weasyprint` 将其转换为 PDF。
8. **前端渲染**: 实时接收并显示日志；通过 WebSocket 接收 `report_generated` 消息，获取报告内容并渲染。

## 6. 关键实现细节

### 6.1 并行抓取与超时
在 `mcp_ddg_service.py` 中，通过 `asyncio.gather` 实现并行抓取，显著缩短了 I/O 等待时间。为每个抓取任务设置了独立的 5 秒超时，避免因个别网站响应慢而阻塞整个流程。

```python
# src/services/mcp_ddg_service.py

async def fetch_html(url: str, timeout: int = 5):
    # ...

tasks = [fetch_html(result["href"]) for result in results[:max_results]]
contents = await asyncio.gather(*tasks)
```

### 6.2 PDF 导出与系统依赖
使用 `WeasyPrint` 替代 `wkhtmltopdf`，因为它更易于通过 Python 控制。但 `WeasyPrint` 依赖 `Pango` 等底层库。在 macOS 上，需要通过 `brew install pango` 来安装，否则应用启动时会因找不到 `gobject-2.0` 动态链接库而抛出 `OSError`。

### 6.3 前端交互优化
- **固定高度与滚动**: 为报告和日志区域的容器 `.card` 添加了 `max-height` 和 `overflow: hidden`，并为内容区域 `.report-container` 和 `.log-list` 设置了 `overflow-y: auto`，实现了固定高度和内部滚动。
- **日志化提示**: 将原先独立的提示框（如“内容不足”）整合到实时日志流中，简化了 UI，统一了信息出口。

## 7. 依赖清单 (`requirements.txt`)
```
fastapi
uvicorn
python-dotenv
httpx
beautifulsoup4
requests
websockets
weasyprint
```

## 8. 安装与运行
1. **克隆仓库**
2. **安装系统依赖 (macOS)**:
   ```bash
   brew install pango
   ```
3. **创建并激活虚拟环境**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```
4. **安装 Python 依赖**:
   ```bash
   pip install -r requirements.txt
   ```
5. **配置环境变量**: 创建 `.env` 文件，填入 `DOUBAO_API_KEY` 等信息。
6. **运行服务**:
   ```bash
   uvicorn src.app:app --reload
   ```
7. **访问**: 打开浏览器访问 `http://127.0.0.1:8000`。

## 9. 测试与验收
- **单元测试**: 针对 `mcp_ddg_service` 的搜索和抓取功能编写测试用例，Mock 掉外部 HTTP 请求。
- **集成测试**: 运行完整的 `main_flow`，验证从输入到生成 PDF 的全过程。
- **前端验收**: 确认日志实时显示、报告正确渲染、PDF 可下载，以及滚动条功能正常。

## 10. 扩展规划
- **搜索源插件化**: 将 `mcp_ddg_service` 抽象为接口，实现 Bing、SerpAPI 等不同搜索源的插件式切换。
- **报告模板引擎**: 使用 `Jinja2` 等模板引擎，支持用户自定义报告结构和样式。
- **前端框架引入**: 若交互逻辑进一步复杂化，可考虑引入 `Vue.js` 或 `React` 进行重构。