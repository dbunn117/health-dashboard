#!/usr/bin/env python3
"""Login to Glooko and download David's latest CSV export.

Reads GLOOKO_EMAIL/GLOOKO_PASSWORD from Heath's profile .env by default.
Does not print credentials. Saves private health exports under /root/health-data/glooko/raw.
"""
from __future__ import annotations

import os
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

PROFILE_ENV = Path(os.environ.get('HERMES_PROFILE_ENV', '/root/.hermes/profiles/heath/.env'))
RAW_DIR = Path('/root/health-data/glooko/raw')
BASE = 'https://us.my.glooko.com'
API = 'https://us.api.glooko.com'
DEFAULT_GLOOKO_CODE = None  # set GLOOKO_CODE in Heath's .env; also discovered from the app page


def load_env(path: Path = PROFILE_ENV) -> None:
    if not path.exists():
        return
    for line in path.read_text(errors='ignore').splitlines():
        s = line.strip()
        if not s or s.startswith('#') or '=' not in s:
            continue
        k, v = s.split('=', 1)
        os.environ.setdefault(k, v)


def csrf(html: str) -> str:
    m = re.search(r'name="authenticity_token"\s+value="([^"]+)"', html)
    if not m:
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    if not m:
        raise RuntimeError('Could not find login CSRF token')
    return m.group(1)


def safe_extract_zip(zip_path: Path, dest: Path) -> int:
    if dest.exists():
        for p in sorted(dest.rglob('*'), reverse=True):
            if p.is_file() or p.is_symlink():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise RuntimeError(f'Unsafe path in zip: {member.filename}')
            zf.extract(member, dest)
            count += 1
    return count


def main() -> int:
    load_env()
    email = os.environ.get('GLOOKO_EMAIL')
    password = os.environ.get('GLOOKO_PASSWORD')
    if not email or not password:
        print('missing_glooko_credentials', file=sys.stderr)
        return 2

    days = int(os.environ.get('GLOOKO_EXPORT_DAYS', '30'))
    # Use a UTC date range. Glooko's UI/API expects ISO UTC day bounds.
    end_date = datetime.now(timezone.utc).date()
    start_date = end_date - timedelta(days=days - 1)
    start_iso = f'{start_date.isoformat()}T00:00:00.000Z'
    end_iso = f'{end_date.isoformat()}T23:59:59.999Z'

    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 Hermes-Agent/1.0 HealthDashboard',
        'Accept-Language': 'en-US,en;q=0.9',
    })

    r = s.get(f'{BASE}/users/sign_in?locale=en', timeout=30)
    r.raise_for_status()
    token = csrf(r.text)
    post = s.post(
        f'{BASE}/users/sign_in?id=login_form&locale=en',
        data={
            'authenticity_token': token,
            'redirect_to': '',
            'language': 'en',
            'user[email]': email,
            'user[password]': password,
            'commit': 'Log In',
        },
        headers={'Referer': f'{BASE}/users/sign_in?locale=en'},
        allow_redirects=True,
        timeout=45,
    )
    post.raise_for_status()
    if '/users/sign_in' in post.url or 'Invalid Email or password' in post.text or 'Log In with SSO' in post.text:
        raise RuntimeError('Glooko login did not complete; manual MFA/CAPTCHA/unlock may be required')

    # Try to discover patient/glooko code from loaded application HTML/API traces; fall back to observed account code.
    code = os.environ.get('GLOOKO_CODE') or DEFAULT_GLOOKO_CODE
    m = re.search(r'patient=([a-z]+-[a-z]+-\d+)', post.text)
    if m:
        code = m.group(1)

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out = RAW_DIR / f'glooko_export_{start_date.isoformat()}_to_{end_date.isoformat()}.zip'
    url = f'{API}/api/v3/users/export_csv?glookoCode={quote(code)}&startDate={quote(start_iso)}&endDate={quote(end_iso)}'
    er = s.get(
        url,
        headers={
            'Origin': BASE,
            'Referer': f'{BASE}/',
            'Accept': 'application/zip,application/octet-stream,*/*',
        },
        timeout=120,
    )
    er.raise_for_status()
    ctype = er.headers.get('content-type', '')
    if len(er.content) < 1000 or (b'<html' in er.content[:200].lower()):
        raise RuntimeError(f'Glooko export did not return a ZIP-like payload; content-type={ctype}, bytes={len(er.content)}')
    out.write_bytes(er.content)

    # Validate ZIP and extract to the convention used by the dashboard.
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        if not names:
            raise RuntimeError('Glooko export ZIP was empty')
    extracted = RAW_DIR / f'extracted_{start_date.isoformat()}_to_{end_date.isoformat()}'
    count = safe_extract_zip(out, extracted)
    print(f'glooko_export_saved={out}')
    print(f'extracted={extracted}')
    print(f'files={count}')
    print(f'range={start_date.isoformat()}_to_{end_date.isoformat()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
