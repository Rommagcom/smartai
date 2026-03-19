# PDF Document Generator Skill

This dynamic tool creates a styled PDF with pdfkit/wkhtmltopdf from HTML or Markdown input.

## Tool
- Name: generate_pdf_document
- Behavior: returns a JSON string with base64-encoded PDF content.

## Notes
- Input:
	- `content` (required): HTML or Markdown content.
	- `input_format` (optional): `auto`, `html`, `markdown`.
	- `title` (optional): rendered as H1 at the top of the document.
	- `theme` (optional): `clean` or `business`.
	- `css` (optional): additional CSS block appended to default styles.
	- `filename` (optional): output file name.
- If `filename` is not provided, a timestamp-based filename is generated.
- Unsafe filename characters are replaced with `_`.
- Requires `wkhtmltopdf` binary in runtime image (`WKHTMLTOPDF_PATH` is optional).
- Includes page numbering in footer (`[page]/[toPage]`), without cover page, logo, or date.
- Output fields: `type`, `mime_type`, `filename`, `base64`, `size_bytes`.
