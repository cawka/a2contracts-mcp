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

### From a checkout, in its own venv (macOS)

```
git clone git@github.com:cawka/a2contracts-mcp.git
cd a2contracts-mcp
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/a2contracts-mcp login --url https://contracts.a2cons.com   # email, password, MFA code
.venv/bin/a2contracts-mcp check                                      # who you are, what you may do
```

Then register it with Claude Code using the venv's own executable (a
full path -- Claude Code launches it outside this shell):

```
claude mcp add a2contracts -- "$PWD/.venv/bin/a2contracts-mcp" serve
```

Claude Desktop / others: point the stdio server at
`/full/path/to/a2contracts-mcp/.venv/bin/a2contracts-mcp` with the
argument `serve`.

### Keeping it up to date

The app and this server move together: a new tool here usually goes
with an endpoint there, so update this whenever the app is deployed (a
tool answering `404` or "unknown field" is the usual sign you're behind).
Releases are git tags (`v0.2.0`, …); `main` is always deployable.

From the checkout:

```
cd ~/Devel/a2contracts-mcp
git pull
.venv/bin/pip install -e .          # picks up new dependencies; code changes need no reinstall
.venv/bin/a2contracts-mcp --version
```

then start a new Claude Code session -- MCP servers are launched per
session, so a running one keeps the old code until restarted (in Claude
Code, `/mcp` shows the connected servers). Your login survives updates
(tokens live in `~/.config`, not in the checkout). With pipx:
`pipx install --force git+https://github.com/cawka/a2contracts-mcp`.

### Or with pipx

```
pipx install git+https://github.com/cawka/a2contracts-mcp
a2contracts-mcp login --url https://contracts.a2cons.com
claude mcp add a2contracts -- a2contracts-mcp serve
```

Tokens are stored in `~/.config/a2contracts-mcp/credentials.json`
(rotated automatically); downloaded sheets are cached in
`~/.cache/a2contracts-mcp/`.

## What it will and will not do

Reads go through `api_get` (any GET the signed-in user is allowed) and
the purpose-built tools below. Writes exist only as named tools, for two
draft areas: plan markups (private drafts until a person publishes) and
draft estimates (divisions and line items while the proposal is not
finalized, and line items on a pending change order). There is no
general-purpose write call, and deliberately no tool to finalize or
un-finalize a proposal, approve or reopen a change order, submit a pay
app, or touch contracts, subcontracts, lien releases or payments --
those record financial agreements and only a person locks or unlocks
them, in the app. The API enforces the same locks server-side (a
finalized proposal's line items are refused), so the tools cannot be
used past that line either.

## Tools

Read: `list_projects`, `list_plan_sheets(project)`, `get_sheet_info(project,
path, page)` (creates the sheet record; reports size, scale in force and
the scale printed on the sheet, units, layers), `get_sheet_text`,
`render_sheet(project, path, page, dpi, x, y, width, height, max_px)` →
PNG + the pixel→point mapping, `list_layers`, `list_markups(sheet_id)`,
`list_symbols`, `api_get(path)` (any GET).

Draft: `create_layer`, `create_markups(sheet_id, layer_id, markups,
image_mapping)` (points in PDF points, or image pixels with the mapping
`render_sheet` returned), `update_markup`, `delete_markups`,
`clear_my_markups`, `set_sheet_scale` (needs publish rights).

Draft estimates: `get_estimate(project)` (divisions, line items, pending
change orders, totals, and whether it is still editable),
`create_division(project, name, items, csi_code)`, `update_division`,
`delete_division`, `create_line_items(division_id, items,
change_order_id)`, `update_line_item(id, patch)`, `delete_line_items`.
Item shape: `{title, description, qty, unit, unit_cost, cost_type
(material|labor|subcontractor|equipment|other), is_allowance}`.

Typical flow: `list_projects` → `list_plan_sheets` → `get_sheet_info` →
`render_sheet` (whole page at ~40 dpi to orient, then crops at 100–150
dpi) → `create_markups` with the crop's `image_mapping` → the person
reviews in the app.

Stamps: `create_markups` with `kind: "stamp"`, `meta: {"symbol": id}` and
`style: {"size": 18}` places a library glyph (ids from `list_symbols`);
stamps count by `subject` like the Count tool. `clear_my_markups(layer_id,
sheet_id)` withdraws this account's own markups on a layer in one call.
