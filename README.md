# A2 Contracts MCP server

Lets an external agent (Claude Code, Claude Desktop, Gemini, anything
that speaks MCP) work with the app through its own REST API: read
projects and plan sheets, look at rendered sheet images, and **draft**
plan markups for a person to review and publish in the app.

## Safety model

- The server signs in as a normal user with a device token. Every tool is
  an ordinary API call as that user, so the app's RBAC decides what it can
  do. Give the agent's account `View plans` + `Mark up plans` and **not**
  `Publish markups`: everything it draws is private until a person
  publishes it. There is deliberately no publish tool.
- Nothing touches the database or the server directly; the server runs on
  your own machine.
- Drafts go on a layer you choose (e.g. "AI suggestions", created hidden
  from clients); review them in the plan viewer, delete the misses,
  publish the rest.

## Setup

Runs entirely on your own machine and talks to the app only through its
HTTPS API, signed in as you. Nothing is installed on the server.

```
pipx install git+https://github.com/<org>/a2contracts-mcp      # or: pip install ... in a venv of your own
a2contracts-mcp login --url https://contracts.a2cons.com      # email, password, MFA code
a2contracts-mcp check                                         # who you are, what you may do
```

Tokens are stored in `~/.config/a2contracts-mcp/credentials.json`
(rotated automatically); downloaded sheets are cached in
`~/.cache/a2contracts-mcp/`.

Claude Code:

```
claude mcp add a2contracts -- a2contracts-mcp serve
```

Claude Desktop / others: run `a2contracts-mcp serve` as a stdio server.

From a checkout: `python3 -m venv .venv && .venv/bin/pip install -e .`
and use `.venv/bin/a2contracts-mcp` in place of the bare command.

## Tools

Read: `list_projects`, `list_plan_sheets(project)`, `get_sheet_info(project,
path, page)` (creates the sheet record; reports size, scale in force and
the scale printed on the sheet, units, layers), `get_sheet_text`,
`render_sheet(project, path, page, dpi, x, y, width, height, max_px)` →
PNG + the pixel→point mapping, `list_layers`, `list_markups(sheet_id)`,
`api_get(path)` (any GET).

Draft: `create_layer`, `create_markups(sheet_id, layer_id, markups,
image_mapping)` (points in PDF points, or image pixels with the mapping
`render_sheet` returned), `update_markup`, `delete_markups`,
`set_sheet_scale` (needs publish rights).

Typical flow: `list_projects` → `list_plan_sheets` → `get_sheet_info` →
`render_sheet` (whole page at ~40 dpi to orient, then crops at 100–150
dpi) → `create_markups` with the crop's `image_mapping` → the person
reviews in the app.
