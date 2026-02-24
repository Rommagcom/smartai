from __future__ import annotations

import base64
from io import BytesIO

from fpdf.fpdf import FPDF


class PdfService:
    def create_pdf_base64(self, title: str, content: str, filename: str = "document.pdf") -> dict:
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_title(title)

        pdf.set_font("Helvetica", "B", 16)
        pdf.multi_cell(0, 10, title)
        pdf.ln(2)

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
