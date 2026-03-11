from __future__ import annotations

import base64
import logging
import os
import re
from io import BytesIO

import pdfkit

logger = logging.getLogger(__name__)

_WKHTMLTOPDF_PATH = os.environ.get("WKHTMLTOPDF_PATH", "")

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    body {{ font-family: 'DejaVu Sans', 'Liberation Sans', Arial, sans-serif; font-size: 12px; line-height: 1.5; margin: 24px; color: #1f2937; }}
    h1 {{ font-size: 22px; margin: 0 0 14px 0; color: #0f172a; border-bottom: 1px solid #e5e7eb; padding-bottom: 6px; }}
    h2 {{ font-size: 16px; margin: 18px 0 8px 0; color: #111827; }}
    h3 {{ font-size: 14px; margin: 14px 0 6px 0; color: #1f2937; }}
    p {{ margin: 8px 0; }}
    ul, ol {{ margin: 6px 0 10px 22px; }}
    li {{ margin: 3px 0; }}
    table {{ width: 100%; border-collapse: collapse; margin: 10px 0 14px 0; font-size: 11px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 6px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f3f4f6; font-weight: 700; color: #111827; }}
    blockquote {{ margin: 10px 0; padding: 8px 10px; background: #f9fafb; border-left: 3px solid #9ca3af; color: #374151; }}
    code {{ font-family: 'DejaVu Sans Mono', 'Courier New', monospace; background: #f3f4f6; padding: 1px 4px; border-radius: 3px; }}
    pre {{ white-space: pre-wrap; word-wrap: break-word; background: #f8fafc; border: 1px solid #e5e7eb; padding: 8px; border-radius: 4px; }}
</style>
</head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>
"""


def _escape_html(text: str) -> str:
    """Escape HTML special characters."""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    return text


def _apply_inline_formatting(text: str) -> str:
    """Apply basic inline markdown-like formatting on already escaped text."""
    out = str(text or "")
    # Bold: **text**
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    # Inline code: `text`
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return out


def _is_table_delimiter(line: str) -> bool:
    core = line.strip().strip("|").strip()
    if not core:
        return False
    parts = [p.strip() for p in core.split("|")]
    if not parts:
        return False
    return all(bool(re.fullmatch(r":?-{3,}:?", p)) for p in parts)


def _parse_table_row(line: str) -> list[str]:
    core = line.strip().strip("|")
    return [cell.strip() for cell in core.split("|")]


def _table_html(lines: list[str]) -> str:
    rows = [_parse_table_row(line) for line in lines if line.strip()]
    if not rows:
        return ""
    header = rows[0]
    data_rows = rows[1:]
    if data_rows and _is_table_delimiter(lines[1]):
        data_rows = rows[2:]

    thead = "".join(f"<th>{_apply_inline_formatting(_escape_html(cell))}</th>" for cell in header)
    body_rows: list[str] = []
    for row in data_rows:
        tds = "".join(f"<td>{_apply_inline_formatting(_escape_html(cell))}</td>" for cell in row)
        body_rows.append(f"<tr>{tds}</tr>")
    tbody = "".join(body_rows)
    return f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>"


def _content_to_html(content: str) -> str:
    """Convert markdown-like text into readable HTML blocks."""
    raw_lines = str(content or "").splitlines()
    if not raw_lines:
        return "<p></p>"

    blocks: list[str] = []
    paragraph_buffer: list[str] = []
    list_buffer: list[str] = []
    list_type: str = ""
    table_buffer: list[str] = []

    def flush_paragraph() -> None:
        nonlocal paragraph_buffer
        if not paragraph_buffer:
            return
        joined = " ".join(line.strip() for line in paragraph_buffer if line.strip())
        if joined:
            escaped = _apply_inline_formatting(_escape_html(joined))
            blocks.append(f"<p>{escaped}</p>")
        paragraph_buffer = []

    def flush_list() -> None:
        nonlocal list_buffer, list_type
        if not list_buffer or not list_type:
            list_buffer = []
            list_type = ""
            return
        tag = "ul" if list_type == "ul" else "ol"
        items = "".join(f"<li>{_apply_inline_formatting(_escape_html(item.strip()))}</li>" for item in list_buffer)
        blocks.append(f"<{tag}>{items}</{tag}>")
        list_buffer = []
        list_type = ""

    def flush_table() -> None:
        nonlocal table_buffer
        if not table_buffer:
            return
        table_html = _table_html(table_buffer)
        if table_html:
            blocks.append(table_html)
        table_buffer = []

    for line in raw_lines:
        stripped = line.strip()

        if not stripped:
            flush_table()
            flush_list()
            flush_paragraph()
            continue

        if "|" in stripped and stripped.count("|") >= 2:
            flush_paragraph()
            flush_list()
            table_buffer.append(stripped)
            continue
        flush_table()

        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1)) + 1
            level = 2 if level < 2 else (3 if level > 3 else level)
            title = _apply_inline_formatting(_escape_html(heading.group(2).strip()))
            blocks.append(f"<h{level}>{title}</h{level}>")
            continue

        quote = re.match(r"^>\s*(.+)$", stripped)
        if quote:
            flush_paragraph()
            flush_list()
            quote_text = _apply_inline_formatting(_escape_html(quote.group(1).strip()))
            blocks.append(f"<blockquote>{quote_text}</blockquote>")
            continue

        ul_match = re.match(r"^(?:[-*•])\s+(.+)$", stripped)
        if ul_match:
            flush_paragraph()
            if list_type not in {"", "ul"}:
                flush_list()
            list_type = "ul"
            list_buffer.append(ul_match.group(1))
            continue

        ol_match = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if ol_match:
            flush_paragraph()
            if list_type not in {"", "ol"}:
                flush_list()
            list_type = "ol"
            list_buffer.append(ol_match.group(1))
            continue

        paragraph_buffer.append(stripped)

    flush_table()
    flush_list()
    flush_paragraph()

    return "\n".join(blocks) if blocks else f"<p>{_escape_html(content)}</p>"


class PdfService:
    def __init__(self) -> None:
        if _WKHTMLTOPDF_PATH:
            self._config = pdfkit.configuration(wkhtmltopdf=_WKHTMLTOPDF_PATH)
        else:
            self._config = None
        self._options = {
            "encoding": "UTF-8",
            "page-size": "A4",
            "margin-top": "15mm",
            "margin-right": "15mm",
            "margin-bottom": "15mm",
            "margin-left": "15mm",
            "quiet": "",
        }

    def create_pdf_base64(self, title: str, content: str, filename: str = "document.pdf") -> dict:
        html = _HTML_TEMPLATE.format(
            title=_escape_html(title),
            body=_content_to_html(content),
        )

        kwargs: dict = {"options": self._options}
        if self._config:
            kwargs["configuration"] = self._config

        pdf_bytes: bytes = pdfkit.from_string(html, False, **kwargs)

        return {
            "file_name": filename,
            "mime_type": "application/pdf",
            "file_base64": base64.b64encode(pdf_bytes).decode("utf-8"),
            "size_bytes": len(pdf_bytes),
        }


pdf_service = PdfService()
