"""A thin client over the app's REST API using a device token (the same
Bearer scheme the native app uses -- docs/mobile-api-reference.md,
"Device-token auth"). Refreshes the token pair once on a 401 and saves
the rotated pair. Every call is subject to the app's own RBAC: the
server can do exactly what the signed-in user's role allows, nothing
more -- that is the whole safety model (see README)."""

import httpx

from . import config


class ApiError(RuntimeError):
    def __init__(self, status: int, detail):
        super().__init__(f'HTTP {status}: {detail}')
        self.status = status
        self.detail = detail


class ApiClient:
    def __init__(self):
        self.creds = config.load()
        if not self.creds.get('api_url') or not self.creds.get('access_token'):
            raise RuntimeError('Not signed in. Run: a2contracts-mcp login --url https://contracts.a2cons.com')
        self.base = self.creds['api_url'].rstrip('/')
        self.http = httpx.Client(timeout=60)

    def _headers(self) -> dict:
        return {'Authorization': f"Bearer {self.creds['access_token']}", 'Accept': 'application/json'}

    def _refresh(self) -> bool:
        refresh = self.creds.get('refresh_token')
        if not refresh:
            return False
        r = self.http.post(f'{self.base}/api/auth/token/refresh/', json={'refresh_token': refresh})
        if r.status_code != 200:
            return False
        pair = r.json()
        self.creds.update({'access_token': pair['access_token'], 'refresh_token': pair['refresh_token']})
        config.save(self.creds)
        return True

    def request(self, method: str, path: str, *, params=None, json=None, retry=True):
        r = self.http.request(method, f'{self.base}{path}', params=params, json=json, headers=self._headers())
        if r.status_code == 401 and retry and self._refresh():
            return self.request(method, path, params=params, json=json, retry=False)
        if r.status_code >= 400:
            try:
                detail = r.json()
            except ValueError:
                detail = r.text[:500]
            raise ApiError(r.status_code, detail)
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    def get(self, path, params=None):
        return self.request('GET', path, params=params)

    def post(self, path, json=None, params=None):
        return self.request('POST', path, json=json, params=params)

    def patch(self, path, json=None):
        return self.request('PATCH', path, json=json)

    def delete(self, path):
        return self.request('DELETE', path)

    def download(self, url: str) -> bytes:
        r = self.http.get(url, follow_redirects=True)
        r.raise_for_status()
        return r.content

    def get_bytes(self, path: str, retry=True) -> bytes:
        """An authenticated binary GET of an app path (a stored plan
        sheet's PDF, /api/plan-sheets/<id>/file.pdf)."""
        r = self.http.get(f'{self.base}{path}', headers={'Authorization': f"Bearer {self.creds['access_token']}"})
        if r.status_code == 401 and retry and self._refresh():
            return self.get_bytes(path, retry=False)
        if r.status_code >= 400:
            raise ApiError(r.status_code, r.text[:500])
        return r.content
