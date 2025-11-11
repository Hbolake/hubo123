import os
from typing import Tuple
import bleach
from markdown import markdown
from weasyprint import HTML


def md_to_html_body(md_text: str) -> str:
    """Convert Markdown to sanitized HTML body fragment with code highlighting classes.
    We enable 'extra' and 'codehilite' extensions to generate richer HTML.
    """
    raw_html = markdown(md_text or "", extensions=['extra', 'codehilite'])
    allowed_tags = [
        'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'ul', 'ol', 'li', 'blockquote', 'pre', 'code',
        'table', 'thead', 'tbody', 'tr', 'th', 'td',
        'em', 'strong', 'a', 'img', 'span', 'div'
    ]
    allowed_attrs = {
        '*': ['class'],
        'a': ['href', 'title', 'target', 'rel'],
        'img': ['src', 'alt', 'title'],
        'code': ['class'],
        'span': ['class'],
        'div': ['class'],
    }
    cleaned = bleach.clean(raw_html, tags=allowed_tags, attributes=allowed_attrs, strip=True)
    return cleaned


def build_full_html(body_html: str, title: str) -> str:
    """Wrap body fragment into a full HTML document with unified screen+print styles.
    Includes running header/footer for WeasyPrint with A4 page and 22mm margins.
    """
    title = title or "报告"
    css = f"""
    :root {{
      --fg: #111;
      --fg-2: #222;
      --fg-3: #444;
      --muted: #666;
      --border: #ddd;
      --shade: #f5f5f5;
    }}
    @page {{
      size: A4;
      margin: 22mm;
      @top-center {{
        content: element(page-header);
      }}
      @bottom-center {{
        content: counter(page) ' / ' counter(pages);
      }}
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', Arial, 'Noto Sans CJK SC', sans-serif;
      color: var(--fg);
      line-height: 1.7;
    }}
    .page-header {{
      position: running(page-header);
      font-size: 12px;
      color: var(--muted);
      border-bottom: 1px solid var(--border);
      padding-bottom: 4px;
      margin-bottom: 12px;
    }}
    article {{
      font-size: 15px;
    }}
    h1 {{ font-size: 24px; color: var(--fg-2); margin: 0 0 12px; }}
    h2 {{ font-size: 20px; color: var(--fg-2); margin: 20px 0 8px; }}
    h3 {{ font-size: 16px; color: var(--fg-2); margin: 16px 0 6px; }}
    p {{ margin: 8px 0; }}
    ul, ol {{ margin: 8px 0 8px 20px; }}
    blockquote {{
      border-left: 4px solid var(--border);
      background: #fafafa;
      margin: 8px 0; padding: 8px 12px; color: var(--fg-3);
    }}
    pre, code {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace;
    }}
    pre {{
      background: #f6f8fa; border: 1px solid var(--border);
      padding: 10px; border-radius: 6px; overflow: auto;
    }}
    /* Pygments-like minimal theme for codehilite */
    .codehilite {{ background: #f6f8fa; border: 1px solid var(--border); padding: 10px; border-radius: 6px; }}
    .codehilite .k {{ color: #005cc5; }}
    .codehilite .s {{ color: #032f62; }}
    .codehilite .c {{ color: #6a737d; }}
    .codehilite .nf {{ color: #d73a49; }}
    table {{
      border-collapse: collapse; width: 100%; font-size: 14px; margin: 12px 0;
    }}
    th, td {{ border: 1px solid var(--border); padding: 8px; text-align: left; }}
    tbody tr:nth-child(even) {{ background: var(--shade); }}
    a {{ color: #222; text-decoration: underline; text-decoration-color: #999; }}
    @media screen {{
      .page-header {{ display: none; }}
    }}
    """

    html = f"""
    <html>
    <head>
      <meta charset=\"utf-8\" />
      <title>{bleach.clean(title)}</title>
      <style>{css}</style>
    </head>
    <body>
      <header class=\"page-header\"><div>{bleach.clean(title)}</div></header>
      <article>{body_html}</article>
    </body>
    </html>
    """
    return html


def html_to_pdf(full_html: str, output_path: str):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    HTML(string=full_html).write_pdf(output_path)