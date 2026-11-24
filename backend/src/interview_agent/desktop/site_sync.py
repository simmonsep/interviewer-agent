"""Local-only site progress sync.

The desktop host starts a browser window with its own profile on the user's
machine. The user signs in to the job site there, in the real site, and this
module reads the DOM of the page the user is looking at through a loopback-only
DevTools port. Nothing is sent anywhere: no credential, cookie, token or page
body is stored by the application, and sync results are never written to the
database - the UI asks the user to confirm each row before calling the ordinary
application progress endpoint.

This is deliberately not a crawler: no self-built HTTP requests to job sites,
no automatic navigation, no pagination, no scheduled background polling, no
captcha or risk-control bypass. With no signed-in human present it returns
nothing.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from urllib.request import Request as UrlRequest, urlopen

from fastapi import APIRouter, Request
from interview_agent.api.dependencies import PrincipalDep, errors
from pydantic import Field

from interview_agent.contracts.common import Contract
from interview_agent.storage.errors import ApiError

STATUSES = {'DRAFT', 'SUBMITTED', 'INTERVIEW', 'OFFER', 'REJECTED', 'WITHDRAWN', 'UNKNOWN'}
CONFIG_NAME = 'tracking-adapters.json'
PROFILE_NAME = 'site-sync-profile'
MAX_ROWS = 200

_lock = threading.Lock()
_bridge: dict | None = None


class SiteSyncError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# --------------------------------------------------------------------------- adapters

def _default_adapters() -> list[dict]:
    demo_url = os.environ.get('INTERVIEW_TRACKING_DEMO_URL', 'http://127.0.0.1:8899/applications.html')
    parsed = urlparse(demo_url)
    return [
        {
            'id': 'local-demo', 'label': '本机演示站点（验收用）', 'entry_url': demo_url,
            'host': parsed.netloc, 'row': '.application-row', 'company': '.company',
            'title': '.title', 'status': '.status', 'link': 'a.job-link',
            'status_map': {'已投递': 'SUBMITTED', '笔试': 'INTERVIEW', '面试': 'INTERVIEW',
                           '不合适': 'REJECTED', '未通过': 'REJECTED', '录用': 'OFFER'},
            'verified': True,
        },
        {
            'id': 'my-site', 'label': '自定义站点（待填写选择器）',
            'entry_url': 'https://example.com/', 'host': 'example.com',
            'row': '', 'company': '', 'title': '', 'status': '', 'link': '',
            'status_map': {}, 'verified': False,
        },
    ]


def _config_path(request: Request) -> Path:
    model_config = getattr(request.app.state, 'model_config_path', None)
    if model_config is not None:
        return Path(model_config).parent / CONFIG_NAME
    override = os.environ.get('INTERVIEW_TRACKING_CONFIG')
    return Path(override) if override else Path('configs') / CONFIG_NAME


def _data_dir(request: Request) -> Path:
    # An explicit per-run directory keeps acceptance runs away from a real
    # user's profile and avoids deleting an in-use browser profile.
    override = os.environ.get('INTERVIEW_TRACKING_DATA_DIR')
    if override:
        return Path(override)
    model_config = getattr(request.app.state, 'model_config_path', None)
    if model_config is not None:
        return Path(model_config).parent
    return Path('.private') / 'site-sync'


def _normalize(raw: dict) -> dict:
    entry = str(raw.get('entry_url', '')).strip()
    parsed = urlparse(entry)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise SiteSyncError('adapter_url_invalid', '站点入口必须是以 http:// 或 https:// 开头的完整网址。')
    # The host is derived, never trusted from the payload: a user cannot point an
    # adapter at site A and let it read a page from site B.
    mapping = raw.get('status_map') or {}
    if not isinstance(mapping, dict):
        raise SiteSyncError('adapter_map_invalid', '状态映射格式不正确。')
    for key, value in mapping.items():
        if value not in STATUSES:
            raise SiteSyncError('adapter_map_invalid', '状态映射的目标状态只能是已定义的状态值。')
    text_fields = ('id', 'label', 'row', 'company', 'title', 'status', 'link')
    out = {key: str(raw.get(key, '') or '').strip()[:300] for key in text_fields}
    out['id'] = out['id'] or f"site-{secrets.token_hex(4)}"
    out['label'] = out['label'] or out['id']
    out['entry_url'] = entry
    out['host'] = parsed.netloc
    out['status_map'] = {str(k)[:80]: str(v) for k, v in mapping.items()}
    out['verified'] = bool(raw.get('verified', False))
    return out


def _load(request: Request) -> list[dict]:
    path = _config_path(request)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        items = value.get('adapters') if isinstance(value, dict) else None
        if not isinstance(items, list) or not items:
            raise ValueError('empty adapter list')
        return [_normalize(item) for item in items if isinstance(item, dict)]
    except (OSError, ValueError, TypeError, SiteSyncError):
        return _default_adapters()


def _save(request: Request, adapters: list[dict]) -> Path:
    path = _config_path(request)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'adapters': adapters}, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def _find(adapters: list[dict], adapter_id: str) -> dict:
    for item in adapters:
        if item['id'] == adapter_id:
            return item
    raise SiteSyncError('adapter_not_found', '找不到这个站点配置，请刷新后重试。', 404)


# --------------------------------------------------------------------------- browser launch

def _browser() -> str:
    override = os.environ.get('INTERVIEW_TRACKING_BROWSER')
    if override and Path(override).exists():
        return override
    for candidate in (
        'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
        'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
        'C:/Program Files/Google/Chrome/Application/chrome.exe',
        'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    ):
        if Path(candidate).exists():
            return candidate
    for name in ('msedge', 'chrome', 'chromium', 'google-chrome'):
        found = shutil.which(name)
        if found:
            return found
    raise SiteSyncError('browser_not_found', '没有找到本机 Edge 或 Chrome 浏览器，无法打开站点窗口。', 404)


def _port_file(profile: Path) -> Path:
    return profile / 'DevToolsActivePort'


def _debug_port(profile: Path) -> int | None:
    try:
        lines = _port_file(profile).read_text(encoding='ascii').splitlines()
        port = int(lines[0].strip())
        if not 1 <= port <= 65535:
            return None
        with urlopen(f'http://127.0.0.1:{port}/json/version', timeout=2) as response:
            json.loads(response.read().decode('utf-8'))
        return port
    except (OSError, ValueError, IndexError):
        return None


def _headless() -> bool:
    return os.environ.get('INTERVIEW_TRACKING_HEADLESS') == '1'


def _open_window(adapter: dict, profile: Path) -> int:
    executable = _browser()
    args = [executable, '--remote-debugging-port=0', '--remote-allow-origins=*',
            '--no-first-run', '--no-default-browser-check',
            f'--user-data-dir={profile}', adapter['entry_url']]
    if _headless():
        args.insert(1, '--headless=new')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL, creationflags=flags)
    for _ in range(60):
        port = _debug_port(profile)
        if port is not None:
            return port
        time.sleep(0.2)
    raise SiteSyncError('browser_start_timeout',
                        '浏览器窗口启动超时。请确认本机 Edge/Chrome 可用，或关闭占用独立配置目录的旧窗口后重试。', 504)


def _active_bridge(profile: Path) -> dict | None:
    global _bridge
    with _lock:
        current = _bridge
    if current is None or current.get('profile') != str(profile):
        return None
    if _debug_port(profile) is None:
        return None
    return current


def disconnect() -> None:
    """Stop reading pages. The browser window, and its local sign-in state, are left alone."""
    global _bridge
    with _lock:
        _bridge = None


# --------------------------------------------------------------------------- minimal CDP websocket client

class _CdpSocket:
    """A dependency-free RFC6455 client, enough for Runtime.evaluate over loopback."""

    def __init__(self, url: str, timeout: float = 8.0):
        parsed = urlparse(url)
        self._sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=timeout)
        self._sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        request = (
            f'GET {parsed.path} HTTP/1.1\r\nHost: {parsed.netloc}\r\n'
            'Upgrade: websocket\r\nConnection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n'
        )
        self._sock.sendall(request.encode('ascii'))
        head = b''
        while b'\r\n\r\n' not in head:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise SiteSyncError('cdp_handshake_failed', '无法连接本机浏览器调试端口。', 502)
            head += chunk
        status, _, self._buffer = head.partition(b'\r\n\r\n')
        if b'101' not in status.split(b'\r\n', 1)[0]:
            raise SiteSyncError('cdp_handshake_failed', '本机浏览器拒绝了调试连接，请关闭窗口后重新打开站点。', 502)
        self._id = 0

    def _read(self, count: int) -> bytes:
        while len(self._buffer) < count:
            chunk = self._sock.recv(max(4096, count - len(self._buffer)))
            if not chunk:
                raise SiteSyncError('cdp_connection_closed', '浏览器调试连接已关闭。', 502)
            self._buffer += chunk
        value, self._buffer = self._buffer[:count], self._buffer[count:]
        return value

    def _send(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack('!H', length)
        else:
            header.append(0x80 | 127)
            header += struct.pack('!Q', length)
        mask = os.urandom(4)
        header += mask
        self._sock.sendall(bytes(header) + bytes(byte ^ mask[i % 4] for i, byte in enumerate(data)))

    def _frame(self) -> tuple[bool, int, bytes]:
        first, second = self._read(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack('!H', self._read(2))[0]
        elif length == 127:
            length = struct.unpack('!Q', self._read(8))[0]
        mask = self._read(4) if second & 0x80 else None
        payload = self._read(length)
        if mask:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return fin, opcode, payload

    def call(self, method: str, params: dict) -> dict:
        self._id += 1
        ident = self._id
        self._send({'id': ident, 'method': method, 'params': params})
        fragments = b''
        while True:
            fin, opcode, payload = self._frame()
            if opcode == 0x8:
                raise SiteSyncError('cdp_connection_closed', '浏览器关闭了调试连接。', 502)
            if opcode == 0x9:
                continue
            if opcode not in (0x0, 0x1, 0x2):
                continue
            fragments += payload
            if not fin:
                continue
            try:
                message = json.loads(fragments.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                fragments = b''
                continue
            fragments = b''
            if message.get('id') == ident:
                return message

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


def _targets(port: int) -> list[dict]:
    try:
        with urlopen(f'http://127.0.0.1:{port}/json/list', timeout=3) as response:
            value = json.loads(response.read().decode('utf-8'))
        return value if isinstance(value, list) else []
    except (OSError, ValueError):
        raise SiteSyncError('cdp_unreachable', '本机浏览器调试端口已不可用，请重新打开站点窗口。', 502)


def _page_target(port: int, host: str, entry_url: str = '') -> dict:
    candidates = [target for target in _targets(port)
                  if target.get('type') == 'page'
                  and urlparse(str(target.get('url', ''))).netloc == host
                  and target.get('webSocketDebuggerUrl')]
    if not candidates:
        raise SiteSyncError('site_page_not_open',
                            f'没有找到已打开的 {host} 页面。请在刚才打开的浏览器窗口里登录，并停留在投递列表页，再点同步。')
    wanted = urlparse(entry_url).path if entry_url else ''
    if wanted:
        exact = [target for target in candidates if urlparse(str(target['url'])).path == wanted]
        if exact:
            return exact[0]
    return candidates[0]


def _open_tab(port: int, url: str) -> bool:
    """Ask the already-running browser to open a tab; newer Chromium requires PUT."""
    try:
        with urlopen(UrlRequest(f'http://127.0.0.1:{port}/json/new?{quote(url, safe="")}',
                                method='PUT'), timeout=3) as response:
            return 200 <= response.status < 300
    except OSError:
        return False


def _evaluate(port: int, target: dict, expression: str):
    client = _CdpSocket(target['webSocketDebuggerUrl'])
    try:
        reply = client.call('Runtime.evaluate', {
            'expression': expression, 'returnByValue': True, 'awaitPromise': False,
        })
    finally:
        client.close()
    if reply.get('error'):
        raise SiteSyncError('cdp_error', '浏览器读取页面失败：' + str(reply['error'].get('message', '')), 502)
    result = reply.get('result', {})
    if result.get('exceptionDetails'):
        raise SiteSyncError('page_script_failed', '页面结构读取失败：选择器可能写错，请用「探测页面结构」核对。', 502)
    return result.get('result', {}).get('value')


_EXTRACT = """(() => {
  const cfg = __CONFIG__;
  const pick = (root, selector) => {
    if (!selector) return '';
    try { const node = root.querySelector(selector); return node ? (node.innerText || node.textContent || '').trim() : ''; }
    catch (error) { return ''; }
  };
  const rows = [];
  let total = 0;
  try {
    for (const node of document.querySelectorAll(cfg.row)) {
      total += 1;
      if (rows.length >= cfg.limit) continue;
      const link = (() => { try { const a = node.querySelector(cfg.link || 'a[href]'); return a ? a.href : ''; } catch (e) { return ''; } })();
      rows.push({company: pick(node, cfg.company), title: pick(node, cfg.title),
                 raw_status: pick(node, cfg.status), link: link});
    }
  } catch (error) { return JSON.stringify({error: String(error), url: location.href, host: location.host, rows: []}); }
  const body = (document.body && document.body.innerText) || '';
  return JSON.stringify({url: location.href, host: location.host, title: document.title,
                         total: total, rows: rows.filter(row => row.company || row.title),
                         login_hint: /请先登录|请登录|立即登录|登录后可|登录后查看|sign in|log in/i.test(body.slice(0, 1500))});
})()"""

_PROBE = """(() => {
  const out = [], seen = new Set();
  for (const node of document.querySelectorAll('[class], li, tr, [role=row], h1, h2, h3, p, a, button, input')) {
    if (out.length >= 30) break;
    const text = (node.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 90);
    const cls = typeof node.className === 'string' ? node.className.trim().slice(0, 120) : '';
    if (text.length < 4) continue;
    const key = node.tagName + '|' + cls + '|' + text.slice(0, 20);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({tag: node.tagName.toLowerCase(), class: cls, id: node.id || '', text: text});
  }
  return JSON.stringify({url: location.href, host: location.host, samples: out});
})()"""


def _expression(template: str, config: dict) -> str:
    return template.replace('__CONFIG__', json.dumps(config, ensure_ascii=False))


# --------------------------------------------------------------------------- public operations

def open_site(request: Request, adapter_id: str) -> dict:
    global _bridge
    adapter = _find(_load(request), adapter_id)
    profile = (_data_dir(request) / PROFILE_NAME).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    executable = _browser()
    with _lock:
        current = _bridge
    if current is not None and current.get('profile') == str(profile) and _debug_port(profile) is not None:
        # An existing window already owns the profile: add a tab to it instead of
        # starting a second browser, so the user's sign-in state is kept.
        port = _debug_port(profile) or current['port']
        if not _open_tab(port, adapter['entry_url']):
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            subprocess.Popen([executable, f'--user-data-dir={profile}', adapter['entry_url']],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL, creationflags=flags)
    else:
        port = _open_window(adapter, profile)
    token = secrets.token_urlsafe(24)
    with _lock:
        _bridge = {'port': port, 'token': token, 'profile': str(profile), 'adapter_id': adapter_id,
                   'started_at': time.time()}
    return {'token': token, 'adapter_id': adapter_id, 'host': adapter['host'],
            'headless': _headless(), 'profile': str(profile),
            'message': f"已在本机浏览器打开 {adapter['label']}。请在该窗口用自己的账号登录，回到工作台点「同步当前页」。"}


def _authorized(request: Request, adapter_id: str, token: str) -> tuple[dict, int]:
    adapter = _find(_load(request), adapter_id)
    profile = (_data_dir(request) / PROFILE_NAME).resolve()
    current = _active_bridge(profile)
    if current is None or not secrets.compare_digest(str(current.get('token', '')), str(token)):
        raise SiteSyncError('tracking_session_expired', '同步会话已失效，请点「打开站点」重新开始。', 409)
    return adapter, int(current['port'])


def _wait_ready(port: int, target: dict, host: str) -> None:
    """A freshly opened tab may still be on about:blank; never read a half-built DOM.

    The host check also prevents a navigation in progress from being mistaken
    for the page the user asked us to read.
    """
    for _ in range(30):
        value = _evaluate(port, target,
                          'JSON.stringify({ready:document.readyState,host:location.host,'
                          'body:(document.body&&document.body.innerText||"").length})')
        try:
            state = json.loads(value) if isinstance(value, str) else {}
        except ValueError:
            state = {}
        if state.get('ready') == 'complete' and state.get('host') == host and state.get('body', 0) > 0:
            return
        time.sleep(0.3)


def _page_payload(port: int, adapter: dict, template: str) -> dict:
    target = _page_target(port, adapter['host'], adapter['entry_url'])
    _wait_ready(port, target, adapter['host'])
    value = _evaluate(port, target, _expression(template, {
        'row': adapter['row'], 'company': adapter['company'], 'title': adapter['title'],
        'status': adapter['status'], 'link': adapter['link'], 'limit': MAX_ROWS,
    }))
    if not isinstance(value, str):
        raise SiteSyncError('page_read_failed', '页面没有返回可读取的内容，请确认停留在投递列表页。', 502)
    try:
        payload = json.loads(value)
    except ValueError:
        raise SiteSyncError('page_read_failed', '页面返回的内容无法解析。', 502)
    if payload.get('error'):
        raise SiteSyncError('selector_invalid', '行选择器无法在当前页面执行：' + str(payload['error'])[:160], 400)
    if payload.get('host') != adapter['host']:
        raise SiteSyncError('host_mismatch', '当前页面与站点配置的域名不一致，已拒绝读取，避免读到别的网站内容。', 409)
    return payload


def sync_page(request: Request, adapter_id: str, token: str) -> dict:
    adapter, port = _authorized(request, adapter_id, token)
    if not adapter['row']:
        raise SiteSyncError('adapter_not_configured',
                            '这个站点还没填写行选择器。点「探测页面结构」看一下真实页面的元素，再保存配置。')
    # SPAs often render the list a moment after readyState=complete. A short
    # retry avoids reporting "no progress" for a page that is still filling in.
    payload: dict = {}
    for attempt in range(3):
        payload = _page_payload(port, adapter, _EXTRACT)
        if payload.get('rows'):
            break
        if attempt < 2:
            time.sleep(1.0)
    warnings: list[str] = []
    rows = []
    mapping = adapter['status_map']
    for raw in payload.get('rows', [])[:MAX_ROWS]:
        text = str(raw.get('raw_status', '')).strip()
        suggested, source = 'UNKNOWN', 'unrecognized'
        if text:
            for keyword, status in mapping.items():
                if keyword and keyword in text:
                    suggested, source = status, 'mapped'
                    break
        if suggested == 'UNKNOWN':
            source = 'unrecognized'
        rows.append({
            'company': str(raw.get('company', '')).strip()[:120],
            'title': str(raw.get('title', '')).strip()[:120],
            'raw_status': text[:120], 'suggested_status': suggested, 'status_source': source,
            'link': str(raw.get('link', '')).strip()[:600],
        })
    if not rows:
        hint = '页面看起来还停在登录页。' if payload.get('login_hint') else ''
        raise SiteSyncError('no_rows_detected',
                            hint + '没有读到投递条目：可能还没登录、当前不在投递列表页，或站点改版导致选择器失效。请核对后重试，或点「探测页面结构」。')
    unrecognized = sum(1 for row in rows if row['status_source'] == 'unrecognized')
    if unrecognized:
        warnings.append(f'有 {unrecognized} 条状态没有匹配到映射，标为「未识别」，请人工判断后再确认。')
    if int(payload.get('total') or 0) > MAX_ROWS:
        warnings.append(f"页面共有 {payload['total']} 条，本次只读取前 {MAX_ROWS} 条，避免误读。")
    if payload.get('login_hint'):
        warnings.append('页面上仍出现登录字样，请确认已经登录成功。')
    return {'adapter_id': adapter_id, 'host': adapter['host'], 'page_url': payload.get('url', ''),
            'captured_at': datetime.now(timezone.utc).isoformat(), 'rows': rows, 'warnings': warnings}


def probe_page(request: Request, adapter_id: str, token: str) -> dict:
    adapter, port = _authorized(request, adapter_id, token)
    payload = _page_payload(port, adapter, _PROBE)
    return {'adapter_id': adapter_id, 'page_url': payload.get('url', ''),
            'samples': payload.get('samples', []),
            'message': '以下是当前页面上的元素样本，用来帮助你填写行选择器与各字段选择器；样本不会保存到数据库。'}


# --------------------------------------------------------------------------- HTTP surface

class AdapterListInput(Contract):
    adapters: list[dict] = Field(default_factory=list)


class AdapterInput(Contract):
    adapter_id: str = Field(max_length=120)
    token: str = Field(default='', max_length=200)


def _principal(request: Request):
    return request.app.state.auth.resolve(request.cookies.get(request.app.state.settings.cookie_name))


def routes():
    router = APIRouter(prefix='/api/desktop/tracking')
    @router.get('/adapters', responses=errors(401))
    async def adapters(request: Request, user:PrincipalDep):
        items = _load(request)
        profile = (_data_dir(request) / PROFILE_NAME).resolve()
        return {'items': items, 'config_path': str(_config_path(request)),
                'profile': str(profile), 'bridge_open': _active_bridge(profile) is not None,
                'headless': _headless()}

    @router.put('/adapters')
    async def save(request: Request, value: AdapterListInput, user:PrincipalDep):
        if not 1 <= len(value.adapters) <= 20:
            raise ApiError('adapter_count_invalid', 400)
        normalized = [_normalize(item) for item in value.adapters]
        ids = [item['id'] for item in normalized]
        if len(set(ids)) != len(ids):
            raise ApiError('adapter_id_duplicate', 400)
        path = _save(request, normalized)
        return {'saved': True, 'config_path': str(path), 'items': normalized}

    @router.post('/open')
    async def open(request: Request, value: AdapterInput, user:PrincipalDep):
        return open_site(request, value.adapter_id)

    @router.post('/sync')
    async def sync(request: Request, value: AdapterInput, user:PrincipalDep):
        return sync_page(request, value.adapter_id, value.token)

    @router.post('/probe')
    async def probe(request: Request, value: AdapterInput, user:PrincipalDep):
        return probe_page(request, value.adapter_id, value.token)

    @router.post('/disconnect', responses=errors(401))
    async def stop(request: Request, user:PrincipalDep):
        disconnect()
        return {'disconnected': True,
                'message': '已停止读取页面。浏览器窗口和它的登录态仍留在本机，可自行关闭；如需彻底清除登录态，删除配置目录里的 site-sync-profile 文件夹。'}

    return router


def attach_error_messages(app) -> None:
    """Expose the Chinese diagnostic text on error responses for this router only."""
    from starlette.responses import JSONResponse

    @app.exception_handler(SiteSyncError)
    async def site_sync_error(request: Request, error: SiteSyncError):
        return JSONResponse({'code': error.code, 'message': error.message}, error.status,
                            headers={'Cache-Control': 'no-store'})
