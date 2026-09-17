"""The MCP tools. Every tool goes through the app's REST API as the
signed-in user, so the app's own RBAC decides what is allowed: an agent
can draft markups only if that user can annotate, and it can never
publish -- there is deliberately no publish tool. A human reviews the
"Suggested" layer in the app and publishes what's right.

Coordinates: every markup point is in PDF points of the page, origin
top-left (the app's own convention). `render_sheet` returns the mapping
from image pixels back to points; `create_markups` accepts pixel
coordinates plus that mapping so an agent can work straight off the
image it was shown.
"""

import json
import logging
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from . import sheets
from .client import ApiClient, ApiError

mcp = MCPServer(
    'a2contracts',
    instructions=(
        'A2 Contracts: construction estimating/project app. Read projects and plan sheets, render sheet '
        'images, and draft plan markups (counts, markers, shapes, measurements) on a layer for a person to '
        'review and publish. Markup coordinates are PDF points (origin top-left); render_sheet tells you '
        'how image pixels map to points. Start with list_projects, then list_plan_sheets, then '
        'get_sheet_info before drawing.'
    ),
)

_client: ApiClient | None = None


def client() -> ApiClient:
    global _client
    if _client is None:
        _client = ApiClient()
    return _client


def _ok(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _err(exc: Exception) -> str:
    if isinstance(exc, ApiError):
        return _ok({'error': exc.detail, 'status': exc.status})
    return _ok({'error': str(exc)})


# --- Reading -----------------------------------------------------------------

@mcp.tool()
def list_projects(include_archived: bool = False) -> str:
    """Projects the signed-in user can see: id (use this `id` everywhere),
    name, project number, client, address."""
    try:
        rows = client().get('/api/projects/', params={'archived': 'true'} if include_archived else None)
        return _ok([
            {
                'id': r['local_id'], 'name': r['name'], 'project_number': r.get('project_number', ''),
                'client': r.get('client_display_name', ''), 'address': ', '.join(p for p in [r.get('address_line1'), r.get('city'), r.get('state')] if p),
                'has_plans_folder': bool(r.get('dropbox_folder_path')),
            }
            for r in rows
        ])
    except Exception as exc:  # noqa: BLE001 -- every tool reports, never raises
        return _err(exc)


@mcp.tool()
def list_plan_sheets(project: int) -> str:
    """The project's plan files (from its Dropbox Plans/ folder) with
    folder, name, path, modified -- plus, for pages the app already
    knows, the sheet record (sheet_id, page, title, scale). Pass `path`
    (and page) to the sheet tools."""
    try:
        files = client().get(f'/api/projects/{project}/plans/')
        known = client().get('/api/plan-sheets/', params={'project': project})
        by_path: dict[str, list] = {}
        for s in known:
            by_path.setdefault(s['dropbox_path'].lower(), []).append({
                'sheet_id': s['id'], 'page': s['page'], 'title': s['title'], 'sheet_number': s['sheet_number'],
                'scale_ratio': s['scale_ratio'], 'scale_source': s['scale_source'],
            })
        return _ok([
            {'folder': f.get('folder'), 'name': f['name'], 'path': f['path'], 'modified': f.get('modified'), 'sheets': by_path.get(f['path'].lower(), [])}
            for f in files
        ])
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def get_sheet_info(project: int, path: str, page: int = 1) -> str:
    """Everything about one plan page: sheet_id (created if needed), page
    count, size in points, the scale in force (and the scale printed on
    the sheet if none is set), measurement units, layers, and how many
    markups are on it. Call this before rendering or drawing."""
    try:
        c = client()
        doc = sheets.open_document(c, project, path)
        page_count = len(doc)
        width, height = sheets.page_size_pt(doc, page)
        try:
            sheet = c.post('/api/plan-sheets/ensure/', json={
                'project': project, 'dropbox_path': path, 'page': page,
                'page_width_pt': round(width, 2), 'page_height_pt': round(height, 2),
            })
        except ApiError as exc:
            if exc.status not in (403, 404):
                raise
            found = c.get('/api/plan-sheets/', params={'project': project, 'dropbox_path': path, 'page': page})
            sheet = found[0] if found else None
        detected = sheets.detect_scale(sheets.page_text(doc, page))
        layers = c.get('/api/plan-layers/', params={'project': project})
        company = c.get('/api/company/')
        markups = c.get('/api/plan-markups/', params={'sheet': sheet['id']}) if sheet else []
        return _ok({
            'sheet_id': sheet['id'] if sheet else None,
            'title': sheet['title'] if sheet else None,
            'page': page, 'page_count': page_count,
            'page_width_pt': round(width, 2), 'page_height_pt': round(height, 2),
            'scale_ratio': float(sheet['scale_ratio']) if sheet and sheet['scale_ratio'] else None,
            'scale_source': sheet['scale_source'] if sheet else '',
            'scale_printed_on_sheet': detected,
            'units': company.get('measurement_units', 'ft_in'),
            'layers': [{'id': l['id'], 'name': l['name'], 'color': l['color'], 'locked': l['is_locked']} for l in layers],
            'markup_count': len(markups),
            'note': 'Points -> real: inches = points / 72 * scale_ratio. Draw on a layer named for suggestions (create_layer) so a person can review before publishing.',
        })
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def get_sheet_text(project: int, path: str, page: int = 1, max_chars: int = 20000) -> str:
    """The page's own text (title block, room names, schedules, notes) --
    from the PDF's text layer, so only for vector sheets. Handy for scale
    detection and for reading legends before counting symbols."""
    try:
        doc = sheets.open_document(client(), project, path)
        text = sheets.page_text(doc, page)
        return text[:max_chars]
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def render_sheet(
    project: int, path: str, page: int = 1, dpi: float = 72,
    x: float | None = None, y: float | None = None, width: float | None = None, height: float | None = None,
    max_px: int = 2000,
) -> list:
    """A PNG of the page, or of a crop of it (x, y, width, height in PDF
    points, origin top-left). Returns the image plus a JSON mapping:
    pt = origin_pt + px * pt_per_px. For a large sheet, look at the whole
    page at low dpi first, then render crops of the areas you care about
    at 100-150 dpi; keep max_px at what your vision input handles well.
    """
    try:
        doc = sheets.open_document(client(), project, path)
        crop = (x, y, width, height) if None not in (x, y, width, height) else None
        png, mapping = sheets.render(doc, page, dpi=dpi, crop_pt=crop, max_px=max_px)  # type: ignore[arg-type]
        return [Image(data=png, format='png'), _ok(mapping)]
    except Exception as exc:  # noqa: BLE001
        return [_err(exc)]


@mcp.tool()
def list_layers(project: int) -> str:
    """The project's markup layers."""
    try:
        return _ok(client().get('/api/plan-layers/', params={'project': project}))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def list_markups(sheet_id: int) -> str:
    """Markups on a sheet the signed-in user can see (published ones, and
    their own drafts): id, layer, kind, points, style, label, subject,
    meta, published_at, created_by_name."""
    try:
        return _ok(client().get('/api/plan-markups/', params={'sheet': sheet_id}))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def api_get(path: str, params: dict | None = None) -> str:
    """Read-only escape hatch: GET any /api/... path of the app as the
    signed-in user (projects, change-orders, payment-applications,
    schedule-tasks, transactions, ...). Query params via `params`."""
    try:
        if not path.startswith('/api/'):
            return _ok({'error': 'path must start with /api/'})
        return _ok(client().get(path, params=params))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# --- Drafting ----------------------------------------------------------------

@mcp.tool()
def create_layer(project: int, name: str, color: str = '#7b3fa0', visible_to_clients: bool = False) -> str:
    """A new markup layer for the project (needs publish rights). Use a
    clear name such as "AI suggestions – outlets" and keep it hidden from
    clients until a person has reviewed it."""
    try:
        return _ok(client().post('/api/plan-layers/', json={'project': project, 'name': name, 'color': color, 'visible_to_clients': visible_to_clients}))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


KINDS = {'pen', 'highlighter', 'line', 'arrow', 'rect', 'ellipse', 'cloud', 'polygon', 'text', 'length', 'multiline', 'area', 'count', 'marker', 'stamp'}


@mcp.tool()
def create_markups(sheet_id: int, layer_id: int, markups: list[dict], image_mapping: dict | None = None) -> str:
    """Draft markups on a sheet (they stay private to the signed-in user
    until a person publishes them in the app -- there is no publish tool).

    Each item: {"kind": one of pen|highlighter|line|arrow|rect|ellipse|
    cloud|polygon|text|length|multiline|area|count|marker|stamp,
    "points": [[x, y], ...], optional "style": {"color", "width", "fill",
    "font_size", "size" (stamp glyph height, pt)}, "label", "subject"
    (counts and stamps with the same subject form one group, e.g.
    "Duplex outlet"), "meta": {"text"} for text, {"title", "notes"} for a
    marker, {"symbol": id} for a stamp (ids: GET /api/plan-symbols/ via
    api_get, e.g. duplex-outlet, switch, recessed-light, smoke-detector)}.
    rect/ellipse/cloud/text take two opposite corners; count/marker/stamp
    take one point; length takes two; area/polygon three or more.

    Points are PDF points unless `image_mapping` (the JSON render_sheet
    returned) is given -- then they are pixels of that image and are
    converted here. Returns the created rows.
    """
    try:
        c = client()
        created = []
        errors = []
        marker_number = None
        for index, item in enumerate(markups):
            kind = item.get('kind')
            if kind not in KINDS:
                errors.append({'index': index, 'error': f'unknown kind {kind!r}'})
                continue
            points = item.get('points') or []
            if image_mapping:
                points = [list(sheets.image_to_pt(float(px), float(py), image_mapping)) for px, py in points]
            points = [[round(float(px), 2), round(float(py), 2)] for px, py in points]
            meta = dict(item.get('meta') or {})
            if kind == 'marker':
                if marker_number is None:
                    existing = c.get('/api/plan-markups/', params={'sheet': sheet_id})
                    marker_number = max((m.get('meta', {}).get('number') or 0) for m in existing if m['kind'] == 'marker') if existing else 0
                marker_number += 1
                meta.setdefault('number', marker_number)
                meta.setdefault('status', 'open')
            body = {
                'sheet': sheet_id, 'layer': layer_id, 'kind': kind, 'geometry': {'points': points},
                'style': item.get('style') or {}, 'label': item.get('label', ''), 'subject': item.get('subject', ''), 'meta': meta,
            }
            try:
                created.append(c.post('/api/plan-markups/', json=body))
            except ApiError as exc:
                errors.append({'index': index, 'error': exc.detail, 'status': exc.status})
        return _ok({'created': [{'id': m['id'], 'kind': m['kind'], 'subject': m['subject']} for m in created], 'errors': errors})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def update_markup(markup_id: int, patch: dict) -> str:
    """Change a markup's points/style/label/subject/meta/layer (own drafts,
    or anything with publish rights)."""
    try:
        allowed = {'geometry', 'style', 'label', 'subject', 'meta', 'layer'}
        body = {k: v for k, v in patch.items() if k in allowed}
        if 'points' in patch:
            body['geometry'] = {'points': patch['points']}
        return _ok(client().patch(f'/api/plan-markups/{markup_id}/', json=body))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def delete_markups(markup_ids: list[int]) -> str:
    """Delete markups (own drafts, or anything with publish rights)."""
    results = []
    for mid in markup_ids:
        try:
            client().delete(f'/api/plan-markups/{mid}/')
            results.append({'id': mid, 'deleted': True})
        except Exception as exc:  # noqa: BLE001
            results.append({'id': mid, 'deleted': False, 'error': str(exc)})
    return _ok(results)


@mcp.tool()
def set_sheet_scale(sheet_id: int, ratio: float, source: str = 'sheet') -> str:
    """Set a sheet's scale as real length per drawing length (1/4" = 1'-0"
    is 48, 1" = 20' is 240, 1:100 is 100). Needs publish rights. Use the
    `scale_printed_on_sheet` get_sheet_info reports, or a known dimension.
    """
    try:
        return _ok(client().patch(f'/api/plan-sheets/{sheet_id}/', json={'scale_ratio': str(ratio), 'scale_source': source if source in ('sheet', 'preset', 'calibrated') else 'preset'}))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def serve() -> None:
    # httpx logs every request at INFO, which would flood the MCP client's log.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    mcp.run()
