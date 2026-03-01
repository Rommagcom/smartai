from __future__ import annotations

import base64
import logging
import os
from io import BytesIO
from pathlib import Path

from fpdf.fpdf import FPDF

logger = logging.getLogger(__name__)

_DEJAVU_FONT_PATH = os.environ.get(
    "PDF_FONT_PATH",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


class PdfService:
    def create_pdf_base64(self, title: str, content: str, filename: str = "document.pdf") -> dict:
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_title(title)

        font_loaded = False
        if Path(_DEJAVU_FONT_PATH).is_file():
            try:
                pdf.add_font("DejaVu", "", _DEJAVU_FONT_PATH, uni=True)
                pdf.add_font("DejaVu", "B", _DEJAVU_FONT_PATH.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"), uni=True)
                font_loaded = True
            except Exception:
                logger.warning("Failed to load DejaVu font, falling back to Helvetica", exc_info=True)

        if font_loaded:
            pdf.set_font("DejaVu", "B", 16)
        else:
            pdf.set_font("Helvetica", "B", 16)
        pdf.multi_cell(0, 10, title)
        pdf.ln(2)

        if font_loaded:
            pdf.set_font("DejaVu", size=11)
        else:
            pdf.set_font("Helvetica", size=11)
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
