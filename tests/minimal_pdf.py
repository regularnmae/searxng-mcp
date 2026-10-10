"""A minimal but valid PDF, built with correct xref offsets.

Used by the fetch_pdf tests so the tool is exercised against a real
document rather than a mock of one. Kept here rather than in a fixture
because the byte layout has to be exact.
"""

from __future__ import annotations

TEXT = "Hello PDF world"
HEADING = "# Domain-Driven Design"


def build_pdf(text: str = TEXT, num_pages: int = 1) -> bytes:
    """Assemble a `num_pages`-page PDF with `text` on each page."""
    objects: list[bytes] = []

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(num_pages))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {num_pages} >>".encode())

    for i in range(num_pages):
        page_no = 3 + 2 * i
        content = f"BT /F1 24 Tf 72 700 Td ({text} page {i + 1}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {page_no + 1} 0 R "
            f"/Resources << /Font << /F1 {3 + 2 * num_pages} 0 R >> >> >>".encode()
        )
        header = b"<< /Length " + str(len(content)).encode() + b" >>"
        objects.append(header + b"\nstream\n" + content + b"\nendstream")

    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)
