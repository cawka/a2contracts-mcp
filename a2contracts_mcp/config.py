"""Where the server keeps its API base URL and device tokens: one JSON
file under the user's config dir, written by `a2contracts-mcp login`
and refreshed in place when the access token rotates. Environment
variables override for one-off runs (A2_API_URL, A2_ACCESS_TOKEN,
A2_REFRESH_TOKEN)."""

import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get('A2_MCP_CONFIG_DIR') or Path.home() / '.config' / 'a2contracts-mcp')
CREDENTIALS_FILE = CONFIG_DIR / 'credentials.json'
CACHE_DIR = Path(os.environ.get('A2_MCP_CACHE_DIR') or Path.home() / '.cache' / 'a2contracts-mcp')


def load() -> dict:
    data: dict = {}
    if CREDENTIALS_FILE.exists():
        data = json.loads(CREDENTIALS_FILE.read_text())
    for key, env in (('api_url', 'A2_API_URL'), ('access_token', 'A2_ACCESS_TOKEN'), ('refresh_token', 'A2_REFRESH_TOKEN')):
        if os.environ.get(env):
            data[key] = os.environ[env]
    return data


def save(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(json.dumps(data, indent=2))
    try:
        CREDENTIALS_FILE.chmod(0o600)
    except OSError:
        pass
