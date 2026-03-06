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
  body {{ font-family: 'DejaVu Sans', 'Liberation Sans', Arial, sans-serif; font-size: 12px; margin: 30px; color: #222; }}
  h1 {{ font-size: 20px; margin-bottom: 12px; }}
  pre {{ white-space: pre-wrap; word-wrap: break-word; }}
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


def _content_to_html(content: str) -> str:
    """Convert plain text content to simple HTML paragraphs."""
    escaped = _escape_html(content)
    paragraphs = []
    for block in re.split(r"\n{2,}", escaped):
        lines = block.strip()
        if lines:
            paragraphs.append(f"<p>{lines.replace(chr(10), '<br>')}</p>")
    return "\n".join(paragraphs) if paragraphs else f"<p>{escaped}</p>"


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
