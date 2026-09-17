"""Plan sheets as an agent needs them: the sheet's stored page (one
PDF per sheet since the app took over plan storage, 2026-09-17 --
/api/plan-sheets/<id>/file.pdf) fetched once and cached locally by the
page's content hash, then rasterized (pypdfium2) at a requested dpi,
optionally cropped -- with the exact mapping back to PDF points, which
is the coordinate space every markup is stored in. Text extraction for
scale detection uses the same file."""

import io
import re

import pypdfium2 as pdfium
from PIL import Image

from . import config
from .client import ApiClient

POINTS_PER_INCH = 72


def get_sheet(client: ApiClient, sheet_id: int) -> dict:
    return client.get(f'/api/plan-sheets/{sheet_id}/')


def _cached_pdf(client: ApiClient, sheet: dict) -> bytes:
    version = sheet.get('current_version') or {}
    if not version:
        raise RuntimeError(f"Sheet {sheet['id']} has no stored page yet (still importing, or a Dropbox-era row -- run the app's Dropbox sync).")
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = config.CACHE_DIR / f"{version['page_sha256']}.pdf"
    if cached.exists():
        return cached.read_bytes()
    data = client.get_bytes(f"/api/plan-sheets/{sheet['id']}/file.pdf")
    cached.write_bytes(data)
    return data


def open_document(client: ApiClient, sheet: dict) -> pdfium.PdfDocument:
    """The sheet's current version as a one-page document (page 1)."""
    return pdfium.PdfDocument(_cached_pdf(client, sheet))


def page_size_pt(doc: pdfium.PdfDocument, page: int) -> tuple[float, float]:
    # pypdfium2's get_size already applies /Rotate, matching pdf.js's
    # viewport (checked against a real rotated sheet: 2592 x 1728 both ways).
    w, h = doc[page - 1].get_size()
    return float(w), float(h)


def page_text(doc: pdfium.PdfDocument, page: int) -> str:
    textpage = doc[page - 1].get_textpage()
    return textpage.get_text_range()


SCALE_PATTERNS = [
    # 1/4" = 1'-0", 3/32"=1', 1-1/2" = 1'-0"
    (re.compile(r'(\d+(?:[ -]\d+/\d+)?|\d+/\d+|\d*\.\d+)\s*["”″]\s*=\s*1\s*[\'’′](?:\s*-?\s*0\s*["”″])?'), 'arch'),
    # 1" = 20'
    (re.compile(r'1\s*["”″]\s*=\s*(\d+)\s*[\'’′]'), 'eng'),
    # 1:100
    (re.compile(r'\b1\s*:\s*(\d{2,4})\b'), 'metric'),
]


def _fraction(text: str) -> float | None:
    t = text.strip().replace(' ', '-')
    m = re.match(r'^(\d+)-(\d+)/(\d+)$', t)
    if m:
        return int(m[1]) + int(m[2]) / int(m[3])
    m = re.match(r'^(\d+)/(\d+)$', t)
    if m:
        return int(m[1]) / int(m[2])
    try:
        return float(t)
    except ValueError:
        return None


def detect_scale(text: str) -> dict | None:
    """Same rule as the app's own measure.ts: the candidate nearest the
    word SCALE wins."""
    normalized = re.sub(r'\s+', ' ', text)
    candidates = []
    for pattern, kind in SCALE_PATTERNS:
        for m in pattern.finditer(normalized):
            if kind == 'arch':
                inches = _fraction(m[1])
                if inches and 0 < inches <= 12:
                    candidates.append((m.start(), 12 / inches, m[0]))
            elif kind == 'eng':
                feet = int(m[1])
                if feet > 1:
                    candidates.append((m.start(), 12 * feet, m[0]))
            else:
                candidates.append((m.start(), float(m[1]), m[0]))
    if not candidates:
        return None
    words = [m.start() for m in re.finditer(r'scale', normalized, re.I)]
    distance = lambda i: min((abs(w - i) for w in words), default=float('inf'))
    candidates.sort(key=lambda c: distance(c[0]))
    _, ratio, match = candidates[0]
    return {'ratio': ratio, 'match': match.strip()}


def render(
    doc: pdfium.PdfDocument, page: int, *, dpi: float = 72, crop_pt: tuple[float, float, float, float] | None = None,
    max_px: int = 2000,
) -> tuple[bytes, dict]:
    """PNG bytes of the page (or a crop of it, in points: x, y, w, h with
    the origin top-left like the viewer) plus the mapping an agent needs
    to turn a pixel in that image back into points:
        pt_x = origin_x + px_x * pt_per_px,  pt_y = origin_y + px_y * pt_per_px
    `dpi` is capped so the longer image side stays within max_px."""
    width_pt, height_pt = page_size_pt(doc, page)
    x0, y0, w, h = crop_pt or (0.0, 0.0, width_pt, height_pt)
    x0 = max(0.0, min(x0, width_pt))
    y0 = max(0.0, min(y0, height_pt))
    w = max(1.0, min(w, width_pt - x0))
    h = max(1.0, min(h, height_pt - y0))
    scale = dpi / POINTS_PER_INCH
    longest = max(w, h) * scale
    if longest > max_px:
        scale = max_px / max(w, h)
    # crop = (left, bottom, right, top) to cut off, in points, after rotation.
    pil = doc[page - 1].render(scale=scale, crop=(x0, height_pt - y0 - h, width_pt - x0 - w, y0)).to_pil()
    buf = io.BytesIO()
    pil.save(buf, format='PNG', optimize=True)
    return buf.getvalue(), {
        'image_width_px': pil.width, 'image_height_px': pil.height,
        'origin_pt': [round(x0, 2), round(y0, 2)], 'pt_per_px': round(1 / scale, 6), 'effective_dpi': round(scale * 72, 2),
        'page_width_pt': round(width_pt, 2), 'page_height_pt': round(height_pt, 2),
    }


def image_to_pt(px: float, py: float, mapping: dict) -> tuple[float, float]:
    ox, oy = mapping['origin_pt']
    r = mapping['pt_per_px']
    return ox + px * r, oy + py * r


def png_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as im:
        return im.size
