"""The MCP tools. Every tool goes through the app's REST API as the
signed-in user, so the app's own RBAC decides what is allowed: an agent
can draft markups only if that user can annotate, and it can never
publish -- there is deliberately no publish tool. A human reviews the
"Suggested" layer in the app and publishes what's right.

The same line holds for estimates (the owner, 2026-09-17): "all draft
modes are fair game, anything that touches 'final' (related to actual
financial agreements) is hard no" -- and locking itself is a human act.
So there are tools to read an estimate and to add/change/remove
divisions and line items while the proposal is a draft (the API refuses
the writes once it is finalized, or on a change order that is no longer
pending), and none to finalize/un-finalize a proposal, approve a change
order, submit a pay app, or touch contracts, subcontracts or releases.

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
        'get_sheet_info before drawing. Draft estimates (proposals) can be read and edited '
        '(divisions, line items) with get_estimate / create_line_items etc. while they are still '
        'drafts. FORBIDDEN, by design and not merely missing: finalizing or un-finalizing a proposal, '
        'approving or reopening a change order, submitting a pay app, and anything on contracts, '
        'subcontracts, lien releases or payments -- those record real financial agreements and only a '
        'person may lock or unlock them, in the app. This server has no general-purpose write call '
        '(api_get is read-only), so do not look for another route; if a task needs one of those steps, '
        'stop and tell the person what to do in the app.'
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
            }
            for r in rows
        ])
    except Exception as exc:  # noqa: BLE001 -- every tool reports, never raises
        return _err(exc)


@mcp.tool()
def list_plan_sheets(project: int, collection: int | None = None) -> str:
    """The project's plan sheets as the app stores them: collections
    (a permit set each; one default) -> folders -> sheets. Each sheet:
    sheet_id (pass this to every sheet tool), sheet_number, title,
    folder, current version (number, date), page size, scale. Pass
    `collection` to list just one."""
    try:
        c = client()
        collections = c.get('/api/plan-collections/', params={'project': project})
        folders = {f['id']: f['name'] for f in c.get('/api/plan-folders/', params={'project': project})}
        sheets_ = c.get('/api/plan-sheets/', params={'project': project})
        out = []
        for col in collections:
            if collection is not None and col['id'] != collection:
                continue
            rows = [s for s in sheets_ if s.get('collection') == col['id']]
            rows.sort(key=lambda s: (folders.get(s.get('folder'), '') if s.get('folder') else '', s.get('sheet_number') or '', s.get('title') or ''))
            out.append({
                'collection_id': col['id'], 'collection': col['name'], 'is_default': col['is_default'],
                'sheets': [
                    {
                        'sheet_id': s['id'], 'sheet_number': s['sheet_number'], 'title': s['title'],
                        'folder': folders.get(s['folder']) if s.get('folder') else None,
                        'version': (s.get('current_version') or {}).get('number'),
                        'version_date': (s.get('current_version') or {}).get('issued_on'),
                        'page_width_pt': s.get('page_width_pt'), 'page_height_pt': s.get('page_height_pt'),
                        'scale_ratio': s['scale_ratio'], 'scale_source': s['scale_source'],
                        'stored': bool(s.get('current_version')),
                    }
                    for s in rows
                ],
            })
        return _ok(out)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def get_sheet_info(sheet_id: int) -> str:
    """Everything about one sheet: number, title, size in points, the
    scale in force (and the scale printed on the sheet if none is set),
    measurement units, layers, and how many markups are on it. Call this
    before rendering or drawing."""
    try:
        c = client()
        sheet = sheets.get_sheet(c, sheet_id)
        doc = sheets.open_document(c, sheet)
        width, height = sheets.page_size_pt(doc, 1)
        detected = sheets.detect_scale(sheets.page_text(doc, 1))
        layers = c.get('/api/plan-layers/', params={'project': sheet['project']})
        company = c.get('/api/company/')
        markups = c.get('/api/plan-markups/', params={'sheet': sheet_id})
        version = sheet.get('current_version') or {}
        return _ok({
            'sheet_id': sheet['id'], 'sheet_number': sheet['sheet_number'], 'title': sheet['title'],
            'version': version.get('number'), 'version_date': version.get('issued_on'),
            'page_width_pt': round(width, 2), 'page_height_pt': round(height, 2),
            'scale_ratio': float(sheet['scale_ratio']) if sheet['scale_ratio'] else None,
            'scale_source': sheet['scale_source'],
            'scale_printed_on_sheet': detected,
            'units': company.get('measurement_units', 'ft_in'),
            'layers': [{'id': l['id'], 'name': l['name'], 'color': l['color'], 'locked': l['is_locked']} for l in layers],
            'markup_count': len(markups),
            'note': 'Points -> real: inches = points / 72 * scale_ratio. Draw on a layer named for suggestions (create_layer) so a person can review before publishing.',
        })
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def get_sheet_text(sheet_id: int, max_chars: int = 20000) -> str:
    """The sheet's own text (title block, room names, schedules, notes)
    -- from the PDF's text layer, so only for vector sheets. Handy for
    scale detection and for reading legends before counting symbols."""
    try:
        c = client()
        doc = sheets.open_document(c, sheets.get_sheet(c, sheet_id))
        return sheets.page_text(doc, 1)[:max_chars]
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def render_sheet(
    sheet_id: int, dpi: float = 72,
    x: float | None = None, y: float | None = None, width: float | None = None, height: float | None = None,
    max_px: int = 2000,
) -> list:
    """A PNG of the sheet, or of a crop of it (x, y, width, height in PDF
    points, origin top-left). Returns the image plus a JSON mapping:
    pt = origin_pt + px * pt_per_px. For a large sheet, look at the whole
    page at low dpi first, then render crops of the areas you care about
    at 100-150 dpi; keep max_px at what your vision input handles well.
    """
    try:
        c = client()
        doc = sheets.open_document(c, sheets.get_sheet(c, sheet_id))
        crop = (x, y, width, height) if None not in (x, y, width, height) else None
        png, mapping = sheets.render(doc, 1, dpi=dpi, crop_pt=crop, max_px=max_px)  # type: ignore[arg-type]
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
def list_symbols() -> str:
    """The stamp library: id, name, category for every symbol a 'stamp'
    markup can place (meta.symbol). Electrical outlets/switches/lights,
    HVAC, plumbing, safety, architectural, general."""
    try:
        return _ok(client().get('/api/plan-symbols/'))
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
def clear_my_markups(layer_id: int, sheet_id: int | None = None) -> str:
    """Delete every markup of the signed-in user's own on a layer -- on one
    sheet, or every sheet of the project when sheet_id is omitted. The way
    to withdraw a batch of suggestions in one go (a person with publish
    rights clearing the same way removes everything on the layer, so use
    this account's own layer). Returns the deleted ids."""
    body: dict = {'layer': layer_id}
    if sheet_id is not None:
        body['sheet'] = sheet_id
    try:
        return _ok(client().post('/api/plan-markups/clear/', json=body))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


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


# --- Draft estimates ---------------------------------------------------------

@mcp.tool()
def get_estimate(project: int) -> str:
    """The project's estimate: proposal status (finalized_at, null while
    it is a draft and editable), every division with its line items
    (id, item_no, title, description, qty, unit, unit_cost, line_total,
    cost_type, is_allowance, change_order), pending change orders (their
    line items are editable too), and the totals."""
    try:
        c = client()
        proj = c.get(f'/api/projects/{project}/')
        divisions = c.get('/api/divisions/', params={'project': project})
        items = c.get('/api/line-items/', params={'project': project})
        change_orders = c.get('/api/change-orders/', params={'project': project})
        by_division: dict[int, list] = {}
        for it in items:
            by_division.setdefault(it['division'], []).append(it)
        keep = ('id', 'item_no', 'title', 'description', 'qty', 'unit', 'unit_cost', 'line_total', 'cost_type', 'is_allowance', 'change_order', 'sort_order')
        out = {
            'project': {'id': project, 'name': proj['name'], 'proposal_finalized_at': proj.get('proposal_finalized_at')},
            'editable': proj.get('proposal_finalized_at') is None,
            'divisions': [
                {
                    'id': d['id'], 'csi_code': d.get('csi_code', ''), 'name': d['name'], 'sort_order': d.get('sort_order'),
                    'line_items': [{k: it.get(k) for k in keep} for it in sorted(by_division.get(d['id'], []), key=lambda x: (x.get('sort_order') or 0, x['id']))],
                }
                for d in sorted(divisions, key=lambda x: (x.get('sort_order') or 0, x['id']))
            ],
            'change_orders': [
                {'id': co['id'], 'number': co.get('number'), 'name': co.get('name'), 'status': co.get('status'), 'editable': co.get('status') == 'pending'}
                for co in change_orders
            ],
        }
        try:
            out['totals'] = c.get('/api/project-rollup/', params={'project': project})
        except Exception:  # noqa: BLE001 -- totals are a nicety
            pass
        return _ok(out)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


# --- Schedule ------------------------------------------------------------------
# Draft-mode work by nature (dates, sequencing, milestones -- nothing here
# records a financial agreement), so the tools may write freely; the app's
# own `schedule` RBAC area still decides per user.

SCHEDULE_TASK_FIELDS = ('name', 'description', 'start_date', 'end_date', 'duration_days', 'is_milestone', 'status', 'ignored', 'sort_order')


@mcp.tool()
def get_schedule(project: int) -> str:
    """The project's construction schedule: every task (id, name,
    line_item + line_item_title when it is a line item's own work, else a
    standalone milestone, start_date, end_date, duration_days,
    is_milestone, status not_started|in_progress|done, ignored,
    subcontract, sort_order) and every dependency (id, task, depends_on,
    dependency_type FS|SS|FF|SF, lag_days). Listing keeps line-item rows
    in sync automatically -- a task exists for each line item already.
    Dates are ISO YYYY-MM-DD."""
    try:
        c = client()
        tasks = c.get('/api/schedule-tasks/', params={'project': project})
        deps = c.get('/api/schedule-task-dependencies/', params={'project': project})
        keep = ('id', 'name', 'line_item', 'line_item_title', 'description', 'start_date', 'end_date', 'duration_days', 'is_milestone', 'status', 'ignored', 'subcontract', 'sort_order')
        return _ok({
            'project': project,
            'tasks': [{k: t.get(k) for k in keep} for t in sorted(tasks, key=lambda t: (t.get('sort_order') or 0, t['id']))],
            'dependencies': [{k: d.get(k) for k in ('id', 'task', 'depends_on', 'dependency_type', 'lag_days')} for d in deps],
            'note': 'Unscheduled tasks have null dates. Set start_date + end_date (or start_date + duration_days); a milestone is a task with is_milestone true. Dependencies: FS = depends_on must finish before task starts (lag_days after).',
        })
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def update_schedule_tasks(updates: list[dict]) -> str:
    """Change several tasks at once. Each entry: {id, and any of name,
    description, start_date, end_date, duration_days, is_milestone,
    status, ignored, sort_order}. Returns the updated rows, plus errors
    per id where one was refused."""
    try:
        c = client()
        done, errors = [], []
        for u in updates:
            task_id = u.get('id')
            patch = {k: v for k, v in u.items() if k in SCHEDULE_TASK_FIELDS}
            if not task_id or not patch:
                errors.append({'id': task_id, 'error': 'id and at least one field are required'})
                continue
            try:
                done.append(c.patch(f'/api/schedule-tasks/{task_id}/', json=patch))
            except ApiError as exc:
                errors.append({'id': task_id, 'error': exc.detail, 'status': exc.status})
        return _ok({'updated': done, 'errors': errors})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def create_milestones(project: int, items: list[dict]) -> str:
    """Add standalone schedule rows that aren't line items: milestones and
    phases (permit issued, inspections, final walkthrough...). Each item:
    {name, start_date?, end_date?, duration_days?, description?,
    is_milestone? (default true)}."""
    try:
        c = client()
        created = []
        for it in items:
            body = {'project': project, 'is_milestone': True, **{k: v for k, v in it.items() if k in SCHEDULE_TASK_FIELDS}}
            created.append(c.post('/api/schedule-tasks/', json=body))
        return _ok(created)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def delete_schedule_tasks(ids: list[int]) -> str:
    """Delete standalone milestones/phases. A line item's own task can't
    be deleted (it is re-created from the estimate); mark it `ignored`
    with update_schedule_tasks instead."""
    try:
        c = client()
        deleted, errors = [], []
        for task_id in ids:
            try:
                row = c.get(f'/api/schedule-tasks/{task_id}/')
                if row.get('line_item'):
                    errors.append({'id': task_id, 'error': "a line item's task -- set ignored instead"})
                    continue
                c.delete(f'/api/schedule-tasks/{task_id}/')
                deleted.append(task_id)
            except ApiError as exc:
                errors.append({'id': task_id, 'error': exc.detail, 'status': exc.status})
        return _ok({'deleted': deleted, 'errors': errors})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def set_dependencies(dependencies: list[dict]) -> str:
    """Link tasks. Each: {task, depends_on, dependency_type? FS|SS|FF|SF
    (default FS), lag_days? (default 0)} -- `task` waits on `depends_on`.
    A link that already exists is updated; a cycle is refused."""
    try:
        c = client()
        existing = {}
        results, errors = [], []
        for d in dependencies:
            task, depends_on = d.get('task'), d.get('depends_on')
            if not task or not depends_on:
                errors.append({'dependency': d, 'error': 'task and depends_on are required'})
                continue
            if task not in existing:
                existing[task] = {row['depends_on']: row for row in c.get('/api/schedule-task-dependencies/', params={'task': task})}
            body = {'task': task, 'depends_on': depends_on, 'dependency_type': d.get('dependency_type', 'FS'), 'lag_days': d.get('lag_days', 0)}
            try:
                prior = existing[task].get(depends_on)
                if prior:
                    results.append(c.patch(f"/api/schedule-task-dependencies/{prior['id']}/", json={'dependency_type': body['dependency_type'], 'lag_days': body['lag_days']}))
                else:
                    results.append(c.post('/api/schedule-task-dependencies/', json=body))
            except ApiError as exc:
                errors.append({'dependency': d, 'error': exc.detail, 'status': exc.status})
        return _ok({'dependencies': results, 'errors': errors})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def delete_dependencies(ids: list[int]) -> str:
    """Remove dependency links by id (from get_schedule)."""
    try:
        c = client()
        deleted, errors = [], []
        for dep_id in ids:
            try:
                c.delete(f'/api/schedule-task-dependencies/{dep_id}/')
                deleted.append(dep_id)
            except ApiError as exc:
                errors.append({'id': dep_id, 'error': exc.detail, 'status': exc.status})
        return _ok({'deleted': deleted, 'errors': errors})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def create_division(project: int, name: str, items: list[dict], csi_code: str = '', sort_order: int | None = None) -> str:
    """Add a division (a section of the estimate, e.g. csi_code "09",
    name "Finishes") to a draft proposal together with its first line
    items (same item shape as create_line_items; at least one -- the app
    does not keep an empty division). Returns the division and the rows."""
    if not items:
        return _ok({'error': 'Give at least one line item; an empty division is removed by the app.'})
    try:
        if client().get(f'/api/projects/{project}/').get('proposal_finalized_at') is not None:
            return _ok({'error': 'This proposal has been finalized -- it is no longer a draft. New scope goes through a change order, which a person creates in the app.'})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    body: dict = {'project': project, 'name': name, 'csi_code': csi_code}
    if sort_order is not None:
        body['sort_order'] = sort_order
    try:
        c = client()
        division = c.post('/api/divisions/', json=body)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    created, errors = [], []
    for item in items:
        try:
            created.append(c.post('/api/line-items/', json={'division': division['id'], **item}))
        except Exception as exc:  # noqa: BLE001
            errors.append({'item': item, 'error': exc.detail if isinstance(exc, ApiError) else str(exc)})
    return _ok({'division': division, 'created': created, 'errors': errors})


@mcp.tool()
def update_division(division_id: int, name: str | None = None, csi_code: str | None = None, sort_order: int | None = None) -> str:
    """Rename / renumber / reorder a division of a draft proposal."""
    body = {k: v for k, v in {'name': name, 'csi_code': csi_code, 'sort_order': sort_order}.items() if v is not None}
    try:
        return _ok(client().patch(f'/api/divisions/{division_id}/', json=body))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def create_line_items(division_id: int, items: list[dict], change_order_id: int | None = None) -> str:
    """Add line items to a division of a draft proposal (or, with
    change_order_id, to a PENDING change order). Each item: {"title",
    optional "description", "qty" (number), "unit" (e.g. "EA", "LF",
    "SF", "LS"), "unit_cost" (number, dollars), "cost_type" one of
    material|labor|subcontractor|equipment|other, "is_allowance" (bool)}.
    Returns the created rows (with item_no and line_total). Refused once
    the proposal is finalized -- a person adds scope through a change
    order then."""
    created, errors = [], []
    c = client()
    for item in items:
        body = {'division': division_id, **item}
        if change_order_id is not None:
            body['change_order'] = change_order_id
        try:
            created.append(c.post('/api/line-items/', json=body))
        except Exception as exc:  # noqa: BLE001
            errors.append({'item': item, 'error': exc.detail if isinstance(exc, ApiError) else str(exc)})
    return _ok({'created': created, 'errors': errors})


@mcp.tool()
def update_line_item(line_item_id: int, patch: dict) -> str:
    """Change fields of a line item on a draft proposal or pending change
    order: any of title, description, qty, unit, unit_cost, cost_type,
    is_allowance, sort_order, division (move). Locked items (finalized
    proposal, decided change order, billed on a pay app) are refused."""
    try:
        return _ok(client().patch(f'/api/line-items/{line_item_id}/', json=patch))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool()
def delete_line_items(line_item_ids: list[int]) -> str:
    """Remove line items from a draft proposal / pending change order.
    Same locks as update_line_item; a division's last item cannot be
    removed (delete the division instead, or replace the item)."""
    results = []
    for lid in line_item_ids:
        try:
            client().delete(f'/api/line-items/{lid}/')
            results.append({'id': lid, 'deleted': True})
        except Exception as exc:  # noqa: BLE001
            results.append({'id': lid, 'deleted': False, 'error': exc.detail if isinstance(exc, ApiError) else str(exc)})
    return _ok(results)


@mcp.tool()
def delete_division(division_id: int) -> str:
    """Remove a division of a draft proposal together with its line items.
    Refused once the proposal is finalized."""
    try:
        client().delete(f'/api/divisions/{division_id}/')
        return _ok({'id': division_id, 'deleted': True})
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def serve() -> None:
    # httpx logs every request at INFO, which would flood the MCP client's log.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    mcp.run()
