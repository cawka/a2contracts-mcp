"""`a2contracts-mcp login --url ...` signs in once (device token, MFA
supported) and stores the token pair; `a2contracts-mcp serve` runs the
MCP server over stdio for Claude Code / Claude Desktop / any MCP client;
`a2contracts-mcp check` prints who you are signed in as."""

import argparse
import getpass
import sys

import httpx

from . import config


def login(args) -> int:
    url = args.url.rstrip('/')
    email = args.email or input('Email: ')
    password = getpass.getpass('Password: ')
    r = httpx.post(f'{url}/api/auth/token/', json={'email': email, 'password': password, 'device_name': 'MCP server'}, timeout=30)
    if r.status_code != 200:
        print(f'Sign-in failed: {r.text[:300]}', file=sys.stderr)
        return 1
    data = r.json()
    if data.get('mfa_required'):
        code = input('MFA code (or a backup code): ').strip()
        body = {'pending_token': data['pending_token']}
        body['backup_code' if len(code) > 6 else 'code'] = code
        r = httpx.post(f'{url}/api/auth/token/mfa/', json=body, timeout=30)
        if r.status_code != 200:
            print(f'MFA failed: {r.text[:300]}', file=sys.stderr)
            return 1
        data = r.json()
    config.save({'api_url': url, 'access_token': data['access_token'], 'refresh_token': data['refresh_token'], 'email': data.get('email', email)})
    print(f'Signed in as {data.get("email", email)}; tokens saved to {config.CREDENTIALS_FILE}')
    return 0


def check(_args) -> int:
    from .client import ApiClient

    c = ApiClient()
    me = c.get('/api/auth/me/')
    role = me.get('role') or {}
    print(f"{me.get('email')} · {me.get('company_name', '')} · role {role.get('name')}")
    print(f"view plans: {role.get('can_view_plans')} · annotate: {role.get('can_annotate_plans')} · publish: {role.get('can_publish_plan_markups')} · share plans: {role.get('can_share_plans')}")
    return 0


def serve(_args) -> int:
    from .server import serve as run

    run()
    return 0


def main() -> None:
    from importlib.metadata import version

    parser = argparse.ArgumentParser(prog='a2contracts-mcp')
    parser.add_argument('--version', action='version', version=f'a2contracts-mcp {version("a2contracts-mcp")}')
    sub = parser.add_subparsers(dest='command', required=True)
    p_login = sub.add_parser('login', help='sign in and store a device token')
    p_login.add_argument('--url', required=True, help='e.g. https://contracts.a2cons.com or http://localhost:8000')
    p_login.add_argument('--email')
    p_login.set_defaults(func=login)
    sub.add_parser('check', help='show who the server is signed in as').set_defaults(func=check)
    sub.add_parser('serve', help='run the MCP server (stdio)').set_defaults(func=serve)
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
