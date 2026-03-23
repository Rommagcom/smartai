from __future__ import annotations

import base64
import html
import importlib
import json
import os
import re
from datetime import UTC, datetime


_DEFAULT_CSS = """
body { font-family: 'DejaVu Sans', 'Liberation Sans', Arial, sans-serif; font-size: 12px; line-height: 1.55; margin: 24px; color: #1f2937; }
h1 { font-size: 22px; margin: 0 0 14px 0; color: #0f172a; border-bottom: 1px solid #e5e7eb; padding-bottom: 6px; }
h2 { font-size: 16px; margin: 18px 0 8px 0; color: #111827; }
h3 { font-size: 14px; margin: 14px 0 6px 0; color: #1f2937; }
p { margin: 8px 0; }
ul, ol { margin: 6px 0 10px 22px; }
li { margin: 3px 0; }
table { width: 100%; border-collapse: collapse; margin: 10px 0 14px 0; font-size: 11px; }
th, td { border: 1px solid #d1d5db; padding: 6px 8px; text-align: left; vertical-align: top; }
th { background: #f3f4f6; font-weight: 700; color: #111827; }
blockquote { margin: 10px 0; padding: 8px 10px; background: #f9fafb; border-left: 3px solid #9ca3af; color: #374151; }
code { font-family: 'DejaVu Sans Mono', 'Courier New', monospace; background: #f3f4f6; padding: 1px 4px; border-radius: 3px; }
pre { white-space: pre-wrap; word-wrap: break-word; background: #f8fafc; border: 1px solid #e5e7eb; padding: 8px; border-radius: 4px; }
""".strip()

_THEME_CSS: dict[str, str] = {
    "clean": _DEFAULT_CSS,
    "business": """
body { font-family: 'DejaVu Sans', 'Liberation Sans', Arial, sans-serif; font-size: 12px; line-height: 1.6; margin: 26px; color: #1f2937; }
h1 { font-size: 24px; margin: 0 0 16px 0; color: #0b1d3a; border-bottom: 2px solid #cbd5e1; padding-bottom: 8px; letter-spacing: 0.2px; }
h2 { font-size: 17px; margin: 20px 0 10px 0; color: #0f172a; }
h3 { font-size: 14px; margin: 16px 0 8px 0; color: #1e293b; }
p { margin: 9px 0; }
ul, ol { margin: 7px 0 11px 24px; }
li { margin: 4px 0; }
table { width: 100%; border-collapse: collapse; margin: 12px 0 16px 0; font-size: 11px; }
th, td { border: 1px solid #cbd5e1; padding: 7px 9px; text-align: left; vertical-align: top; }
th { background: #e2e8f0; font-weight: 700; color: #0f172a; }
blockquote { margin: 12px 0; padding: 9px 12px; background: #f8fafc; border-left: 4px solid #64748b; color: #334155; }
code { font-family: 'DejaVu Sans Mono', 'Courier New', monospace; background: #eef2ff; padding: 1px 4px; border-radius: 3px; }
pre { white-space: pre-wrap; word-wrap: break-word; background: #f8fafc; border: 1px solid #dbe3ef; padding: 10px; border-radius: 4px; }
""".strip(),
}


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", name.strip())
    return cleaned or "document.pdf"


def _detect_input_format(content: str, input_format: str) -> str:
    normalized = (input_format or "auto").strip().lower()
    if normalized in {"html", "markdown"}:
        return normalized
    if normalized != "auto":
        raise ValueError("input_format must be one of: auto, html, markdown")

    text = content.strip().lower()
    html_markers = ("<html", "<body", "<p", "<div", "<h1", "<h2", "<ul", "<ol", "<table", "<br")
    return "html" if any(marker in text for marker in html_markers) else "markdown"


def _normalize_theme(theme: str) -> str:
    value = (theme or "clean").strip().lower()
    if value not in _THEME_CSS:
        allowed = ", ".join(sorted(_THEME_CSS.keys()))
        raise ValueError(f"theme must be one of: {allowed}")
    return value


def _to_html(content: str, input_format: str) -> str:
    format_kind = _detect_input_format(content, input_format)
    if format_kind == "html":
        return content

    try:
        markdown_module = importlib.import_module("markdown")
    except ModuleNotFoundError as exc:
        raise RuntimeError("markdown is not installed. Install dependency: markdown>=3.7") from exc
    markdown_to_html = getattr(markdown_module, "markdown")
    return markdown_to_html(content, extensions=["extra", "sane_lists", "nl2br"])


def _build_html_document(
    title: str | None,
    body_html: str,
    css: str | None,
    theme: str,
) -> str:
    def _normalize_text(value: str) -> str:
        lowered = value.strip().lower()
        return re.sub(r"\s+", " ", lowered)

    def _strip_tags(value: str) -> str:
        return re.sub(r"<[^>]+>", "", value)

    def _leading_h1_text(value: str) -> str | None:
        match = re.match(r"^\s*<h1\b[^>]*>(.*?)</h1>", value, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return None
        return _strip_tags(match.group(1)).strip()

    css_parts = [_THEME_CSS[theme]]

    if css and css.strip():
        css_parts.append(css.strip())

    title_html = ""
    if title:
        existing_h1 = _leading_h1_text(body_html)
        if not existing_h1 or _normalize_text(existing_h1) != _normalize_text(title):
            title_html = f"<h1>{html.escape(title)}</h1>"

    all_css = "\n".join(css_parts)
    return (
        "<!DOCTYPE html>"
        "<html><head><meta charset=\"utf-8\"><style>"
        f"{all_css}"
        "</style></head><body>"
        f"{title_html}{body_html}"
        "</body></html>"
    )


def generate_pdf_document(
    content: str,
    input_format: str = "auto",
    title: str | None = None,
    theme: str = "clean",
    filename: str | None = None,
    css: str | None = None,
) -> str:
    try:
        pdfkit = importlib.import_module("pdfkit")
    except ModuleNotFoundError as exc:
        raise RuntimeError("pdfkit is not installed. Install dependency: pdfkit>=1.0.0") from exc

    selected_theme = _normalize_theme(theme)
    body_html = _to_html(content=content, input_format=input_format)
    html_doc = _build_html_document(
        title=title,
        body_html=body_html,
        css=css,
        theme=selected_theme,
    )

    if filename:
        final_name = _safe_filename(filename)
        if not final_name.lower().endswith(".pdf"):
            final_name += ".pdf"
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        final_name = f"document_{stamp}.pdf"

    wkhtmltopdf_path = os.getenv("WKHTMLTOPDF_PATH", "").strip()
    config = None
    if wkhtmltopdf_path:
        configuration = getattr(pdfkit, "configuration")
        config = configuration(wkhtmltopdf=wkhtmltopdf_path)

    options = {
        "encoding": "UTF-8",
        "page-size": "A4",
        "margin-top": "15mm",
        "margin-right": "15mm",
        "margin-bottom": "18mm",
        "margin-left": "15mm",
        "footer-right": "[page]/[toPage]",
        "footer-font-size": "9",
        "footer-spacing": "4",
        "print-media-type": "",
        "quiet": "",
    }

    from_string = getattr(pdfkit, "from_string")
    kwargs: dict[str, object] = {"options": options}
    if config is not None:
        kwargs["configuration"] = config
    pdf_bytes = from_string(html_doc, False, **kwargs)
    if not isinstance(pdf_bytes, (bytes, bytearray)):
        raise RuntimeError("pdfkit did not return PDF bytes")

    payload = {
        "type": "file",
        "mime_type": "application/pdf",
        "filename": final_name,
        "base64": base64.b64encode(bytes(pdf_bytes)).decode("ascii"),
        "size_bytes": len(bytes(pdf_bytes)),
    }
    return json.dumps(payload, ensure_ascii=True)
