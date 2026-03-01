from __future__ import annotations

import base64
import logging
import os
from io import BytesIO

from fpdf.fpdf import FPDF

logger = logging.getLogger(__name__)

_FONT_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "fonts")
_UNICODE_FONT_FILE = os.path.join(_FONT_DIR, "DejaVuSans.ttf")
_UNICODE_FONT_BOLD_FILE = os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")


class PdfService:
    def _setup_font(self, pdf: FPDF) -> str:
        """Register a Unicode-capable font if available, otherwise fall back to Helvetica."""
        if os.path.isfile(_UNICODE_FONT_FILE):
            pdf.add_font("DejaVu", "", _UNICODE_FONT_FILE, uni=True)
            if os.path.isfile(_UNICODE_FONT_BOLD_FILE):
                pdf.add_font("DejaVu", "B", _UNICODE_FONT_BOLD_FILE, uni=True)
            return "DejaVu"
        logger.warning("Unicode font not found at %s — PDF will use Helvetica (no Cyrillic support)", _UNICODE_FONT_FILE)
        return "Helvetica"

    def create_pdf_base64(self, title: str, content: str, filename: str = "document.pdf") -> dict:
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_title(title)

        font_family = self._setup_font(pdf)

        bold_style = "B" if font_family == "DejaVu" and os.path.isfile(_UNICODE_FONT_BOLD_FILE) else "B"
        pdf.set_font(font_family, bold_style, 16)
        pdf.multi_cell(0, 10, title)
        pdf.ln(2)

        pdf.set_font(font_family, size=11)
        safe_text = content.replace("\t", "    ")
        for line in safe_text.splitlines() or [safe_text]:
            pdf.multi_cell(0, 7, line)

        output = pdf.output(dest="S")
        if isinstance(output, bytearray):
            pdf_bytes = bytes(output)
        elif isinstance(output, str):
            pdf_bytes = output.encode("latin-1", errors="ignore")
        else:
            buffer = BytesIO()
            pdf.output(buffer)
            pdf_bytes = buffer.getvalue()

        return {
            "file_name": filename,
            "mime_type": "application/pdf",
            "file_base64": base64.b64encode(pdf_bytes).decode("utf-8"),
            "size_bytes": len(pdf_bytes),
        }


pdf_service = PdfService()
