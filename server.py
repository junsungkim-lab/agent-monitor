#!/usr/bin/env python3
"""agent-monitor: 상태 서버 (Python 3 표준 라이브러리만 사용, 설치 불필요)

    python3 server.py              # 이 컴퓨터에서만 접속 (127.0.0.1:8790)
    python3 server.py --lan        # 같은 와이파이의 아이패드 등에서도 접속 (토큰 필요)

훅 연결까지 한 번에 하려면 start.py 를 쓴다.

Claude Code / Codex / Gemini CLI 훅이 보낸 이벤트를 에이전트 상태로 바꿔
브라우저 화면에 실시간(SSE)으로 전달한다.
"""
import argparse
import hashlib
import hmac
import importlib.util
import io
import ipaddress
import json
import mimetypes
import os
import queue
import re
import secrets
import socket
import stat
import subprocess
import sys
import threading
import time
import wave
import webbrowser
from collections import OrderedDict
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
from urllib.parse import parse_qs, quote, unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.realpath(os.path.join(ROOT, 'web'))
MAX_BODY = 64 * 1024

SOURCES = ('claude', 'codex', 'gemini', 'custom')
STATES = ('idle', 'working', 'waiting', 'blocked', 'error', 'reported')
ATTENTION = ('waiting', 'blocked', 'error')      # 사람이 봐줘야 하는 상태
MODES = ('quiet', 'nag', 'chaos')

# 알림(Notification) 종류 가운데 "사람 입력이 필요함"을 뜻하는 것
BLOCKING_NOTICES = {
    'permission_prompt', 'elicitation_dialog', 'elicitation_url_dialog',
    'agent_needs_input',          # Claude Code
    'ToolPermission',             # Gemini CLI
}
TOOL_EVENTS = {'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'PostToolBatch',
               'BeforeTool', 'AfterTool', 'ElicitationResult'}


MEDIA_MAX = 4
MEDIA_TYPES = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/quicktime', '.webm': 'video/webm',
               '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp',
               '.gif': 'image/gif'}
MEDIA_FILE_MAX = 4 * 1024 ** 3
MEDIA_URL_RE = re.compile(r'^/media/([0-9a-f]{20})(\.[a-z0-9]{2,5})$')


class MediaFiles:
    """이 컴퓨터에 있는 결과물 파일을 화면에 띄우기 위한 목록.

    경로는 서버 안에만 두고, 화면에는 /media/<임의 id>.<확장자> 주소만 내보낸다.
    이미지·영상 확장자이고 실제로 있는 파일만 받는다.
    """
    KEEP = 500

    def __init__(self):
        self.lock = threading.Lock()
        self.paths = OrderedDict()          # id -> 실제 경로

    def register(self, value):
        """로컬 경로(또는 file:// 주소)를 /media/ 주소로 바꾼다. 받을 수 없으면 ''."""
        if not isinstance(value, str) or not 1 < len(value) <= 4000:
            return ''
        if value.lower().startswith('file://'):
            value = unquote(urlparse(value).path)
        path = os.path.expanduser(value)
        if not os.path.isabs(path):
            return ''
        real = os.path.realpath(path)
        ext = os.path.splitext(real)[1].lower()
        if ext not in MEDIA_TYPES:
            return ''
        try:
            st = os.stat(real)
        except OSError:
            return ''
        if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= MEDIA_FILE_MAX:
            return ''
        mid = hashlib.sha256(('%s|%d|%d' % (real, st.st_mtime_ns, st.st_size)).encode()).hexdigest()[:20]
        with self.lock:
            self.paths[mid] = real
            self.paths.move_to_end(mid)
            while len(self.paths) > self.KEEP:
                self.paths.popitem(last=False)
        return '/media/%s%s' % (mid, ext)

    def known(self, url):
        m = MEDIA_URL_RE.match(url)
        if not m:
            return False
        with self.lock:
            real = self.paths.get(m.group(1))
        return bool(real) and os.path.splitext(real)[1].lower() == m.group(2)

    def lookup(self, url):
        """/media/... 주소에 해당하는 파일 경로. 없으면 ''."""
        if not self.known(url):
            return ''
        with self.lock:
            return self.paths.get(MEDIA_URL_RE.match(url).group(1), '')


MEDIA = MediaFiles()
CTYPE_EXT = {v: k for k, v in MEDIA_TYPES.items() if k not in ('.m4v', '.jpeg')}


def results_dir():
    return os.path.expanduser(CFG['results_dir'])


def short_path(path):
    home = os.path.expanduser('~')
    return '~' + path[len(home):] if home and path.startswith(home + os.sep) else path


DOWNLOAD_LOCK = threading.Lock()
DOWNLOADED = {}                                  # 인터넷 결과물 주소 -> 결과물 폴더에 받아 둔 파일


def result_file(url):
    """화면에 뜬 결과물 주소를 이 컴퓨터의 파일 경로로. 인터넷 주소면 결과물 폴더에 내려받는다.

    화면에 지금 떠 있는 결과물만 다룬다. (아무 주소나 내려받게 하지 않는다)
    """
    if not isinstance(url, str) or not STORE.has_media(url):
        raise LookupError('not a result on the monitor')
    if url.startswith('/media/'):
        path = MEDIA.lookup(url)
        if not path:
            raise LookupError('file is gone')
        return path
    with DOWNLOAD_LOCK:                          # 같은 결과물을 두 번 누르거나 동시에 받아도 한 번만
        known = DOWNLOADED.get(url)
        if known and os.path.isfile(known):
            return known
        name = os.path.basename(unquote(urlparse(url).path)) or 'result'
        name = re.sub(r'[^\w.\-]+', '_', name).strip('._') or 'result'
        stem, ext = os.path.splitext(name)
        folder = results_dir()
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, '.%s.part' % hashlib.sha256(url.encode()).hexdigest()[:16])
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'agent-monitor'})
            with urllib.request.urlopen(req, timeout=60) as r:
                if ext.lower() not in MEDIA_TYPES:
                    ext = CTYPE_EXT.get((r.headers.get('Content-Type') or '').split(';')[0].strip().lower(), '')
                    if not ext:
                        raise ValueError('not an image or video')
                got = 0
                with open(tmp, 'wb') as f:
                    while True:
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        got += len(chunk)
                        if got > MEDIA_FILE_MAX:
                            raise ValueError('file too large')
                        f.write(chunk)
            dest = os.path.join(folder, stem + ext)
            n = 2
            while os.path.exists(dest):          # 이름이 같은 다른 결과물은 덮어쓰지 않는다
                dest = os.path.join(folder, '%s-%d%s' % (stem, n, ext))
                n += 1
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        DOWNLOADED[url] = dest
        return dest


def reveal(path):
    """파일 관리자(Finder 등)에서 그 파일을 선택한 채로 연다."""
    if sys.platform == 'darwin':
        cmd = ['open', '-R', path]
    elif os.name == 'nt':
        cmd = ['explorer', '/select,', path]
    else:
        cmd = ['xdg-open', os.path.dirname(path)]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def clean_media(value, local=False):
    """화면에 띄울 결과물 주소 목록.

    http(s) 주소는 그대로, 이 서버가 이미 아는 /media/ 주소도 그대로 받는다.
    local=True (이 컴퓨터에서 온 요청)일 때만 로컬 파일 경로를 받아 /media/ 주소로 바꾼다.
    """
    items = value if isinstance(value, list) else [value]
    out = []
    for v in items:
        if not isinstance(v, str):
            continue
        v = v.strip()
        if v.lower().startswith(('http://', 'https://')):
            if not 8 < len(v) <= 2000 or any(c in v for c in ' \n\r\t"<>'):
                continue
        elif MEDIA_URL_RE.match(v):
            if not MEDIA.known(v):
                continue
        elif local:
            v = MEDIA.register(v)
            if not v:
                continue
        else:
            continue
        if v not in out:
            out.append(v)
    return out[:MEDIA_MAX]


def looks_like_question(text):
    return text.rstrip(' *_`)"\'”’').endswith(('?', '？'))


def clip(value, limit=80):
    """화면에 나갈 짧은 문자열만 허용한다."""
    if not isinstance(value, str):
        return ''
    value = ' '.join(value.split())
    return value[:limit]


class Store:
    """에이전트 상태 저장소. 모든 변경은 lock 안에서 하고 SSE로 알린다."""

    def __init__(self, sub_ttl=20.0, stale_after=8 * 3600.0):
        self.lock = threading.RLock()
        self.agents = {}            # id -> dict
        self.clients = []           # SSE 구독자 queue
        self.mode = 'nag'
        self.counter = 0            # 입장 순번
        self.sub_ttl = sub_ttl
        self.stale_after = stale_after
        self.preview = True         # 응답 미리보기·명령·결과물 주소를 받아 둘지
        self.result_keep = 120.0    # 결과물이 걸린 서브에이전트를 새 턴에도 남겨 두는 시간(초)

    # ---- 브로드캐스트 -------------------------------------------------
    def _emit(self, evt):
        evt['now'] = time.time()
        data = json.dumps(evt, ensure_ascii=False)
        dead = []
        for q in self.clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            self.clients.remove(q)

    def subscribe(self):
        q = queue.Queue(maxsize=500)
        with self.lock:
            first = json.dumps(self._snapshot_evt(), ensure_ascii=False)
            self.clients.append(q)
        return q, first

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def _snapshot_evt(self):
        return {'type': 'snapshot', 'agents': self.snapshot(), 'mode': self.mode, 'now': time.time(),
                'voice': VOICE.info()}

    def voice_changed(self):
        with self.lock:
            self._emit({'type': 'voice', 'voice': VOICE.info()})

    def has_media(self, url):
        with self.lock:
            return any(url in a['media'] for a in self.agents.values())

    def snapshot(self):
        with self.lock:
            return [dict(a) for a in sorted(self.agents.values(), key=lambda a: a['n'])]

    # ---- 내부 도우미 --------------------------------------------------
    def _free_seat(self):
        used = {a['seat'] for a in self.agents.values()}
        seat = 0
        while seat in used:
            seat += 1
        return seat

    def _upsert(self, aid, source, kind='session', parent=None):
        a = self.agents.get(aid)
        if a is None:
            self.counter += 1
            now = time.time()
            a = {'id': aid, 'source': source, 'kind': kind, 'parent': parent, 'n': self.counter,
                 'seat': self._free_seat(), 'state': 'idle', 'since': now, 'acked': False,
                 'project': '', 'title': '', 'role': '', 'note': '', 'name': '',
                 'text': '', 'ask': False, 'detail': '', 'media': [], 'joined': now, 'seen': now}
            self.agents[aid] = a
            return a, True
        a['seen'] = time.time()
        return a, False

    def _set_state(self, a, state, note=''):
        if a['state'] == state and a['note'] == note:
            return False
        if a['state'] != state:
            a['since'] = time.time()
            a['acked'] = False
            if a['state'] == 'blocked':
                a['detail'] = ''                # 승인이 끝났으니 기다리던 명령은 지운다
        a['state'] = state
        a['note'] = note
        return True

    def _remove(self, aid, reason):
        a = self.agents.pop(aid, None)
        if a is not None:
            self._emit({'type': 'remove', 'id': aid, 'reason': reason})
        return a

    def _remove_tree(self, sess_id, reason):
        for aid in [k for k, v in self.agents.items() if v.get('parent') == sess_id]:
            self._remove(aid, reason)
        self._remove(sess_id, reason)

    # ---- 훅 이벤트 → 상태 ---------------------------------------------
    def apply_hook(self, source, p, local=False):
        """local=True: 이 컴퓨터에서 온 요청. 이때만 결과물 로컬 파일 경로를 받는다."""
        if source not in ('claude', 'codex', 'gemini'):
            raise ValueError('unknown source: %s' % source)
        if not isinstance(p, dict):
            raise ValueError('body must be a JSON object')
        ev = clip(p.get('hook_event_name'), 40)
        sid = clip(p.get('session_id'), 120)
        if not ev or not sid:
            raise ValueError('hook_event_name and session_id are required')
        sub = clip(p.get('agent_id'), 120)
        sess_id = '%s:%s' % (source, sid)

        with self.lock:
            if ev == 'SessionEnd' and not sub:
                self._remove_tree(sess_id, clip(p.get('reason'), 40) or 'ended')
                return

            sess, created = self._upsert(sess_id, source)
            changed = created
            project = clip(p.get('project'), 60)
            title = clip(p.get('session_title'), 60)
            if project and sess['project'] != project:
                sess['project'] = project
                changed = True
            if title and sess['title'] != title:
                sess['title'] = title
                changed = True
            if changed:
                self._emit({'type': 'agent', 'agent': dict(sess)})

            target = sess
            if sub:
                sub_id = sess_id + ':' + sub
                known = sub_id in self.agents
                if ev in ('SubagentStop', 'Stop') and not known:
                    return                      # 모르는 서브에이전트의 종료는 무시
                target, sub_created = self._upsert(sub_id, source, 'subagent', sess_id)
                role = clip(p.get('agent_type'), 40)
                if sub_created:
                    target['role'] = role
                    target['project'] = sess['project']
                    target['state'] = 'working'
                    self._emit({'type': 'agent', 'agent': dict(target)})

            changed = self._absorb(target, ev, p, is_sub=bool(sub), local=local)
            if self._transition(target, sess, ev, p, is_sub=bool(sub)):
                changed = True
            if changed:
                self._emit({'type': 'agent', 'agent': dict(target)})

    def _clear_result(self, a):
        changed = bool(a['text'] or a['ask'] or a['detail'] or a['media'])
        a['text'], a['ask'], a['detail'], a['media'] = '', False, '', []
        return changed

    def _label(self, a, label):
        """도구가 출력한 '[agent-monitor] name: ...' 줄로 타일 이름·역할을 바꾼다."""
        if not isinstance(label, dict):
            return False
        changed = False
        for key, limit in (('name', 30), ('role', 40)):
            value = clip(label.get(key), limit)
            if value and a[key] != value:
                a[key] = value
                changed = True
        text = clip(label.get('text'), 240)               # 도구가 직접 적은 결과 한 줄
        if text and self.preview and (a['text'] != text or a['ask']):
            a['text'], a['ask'] = text, False
            changed = True
        return changed

    def _add_media(self, a, value, local):
        fresh = [m for m in clean_media(value, local) if m not in a['media']]
        if not fresh:
            return False
        a['media'] = (fresh + a['media'])[:MEDIA_MAX]
        return True

    def _absorb(self, a, ev, p, is_sub, local=False):
        """이벤트에 딸려 온 미리보기(응답 요약, 기다리는 명령, 결과물)를 타일에 담는다."""
        if ev in ('UserPromptSubmit', 'BeforeAgent') and not is_sub:
            # 새 턴: 지난 결과와, 보고를 마치고 남아 있던 서브에이전트를 치운다.
            # 단, CLI 가 스스로 넣은 알림(백그라운드 서브에이전트 완료 등)은 사람의 새 명령이 아니다.
            # 그리고 결과물이 걸린 서브에이전트는 방금 보고했으면 남겨 둔다. (화면의 × 로 내릴 수 있다)
            if not p.get('auto_prompt'):
                now = time.time()
                for sid in [k for k, v in self.agents.items() if v.get('parent') == a['id'] and v['state'] == 'reported'
                            and (not v['media'] or now - v['since'] > self.result_keep)]:
                    self._remove(sid, 'reported')
            return self._clear_result(a)
        changed = self._label(a, p.get('label'))
        if not self.preview:
            return changed
        if ev in TOOL_EVENTS:
            changed = self._add_media(a, p.get('media'), local) or changed
        elif ev in ('Stop', 'SubagentStop', 'AfterAgent'):
            head, tail = clip(p.get('head'), 240), clip(p.get('tail'), 240)
            ask = bool(tail) and looks_like_question(tail) and not is_sub and ev != 'SubagentStop'
            text = tail if ask else (head or tail)
            if text and (text != a['text'] or ask != a['ask']):
                a['text'], a['ask'] = text, ask
                changed = True
            changed = self._add_media(a, p.get('media'), local) or changed
        elif ev in ('PermissionRequest', 'Elicitation') or (
                ev == 'Notification' and clip(p.get('notification_type'), 40) in BLOCKING_NOTICES and not a['detail']):
            detail = clip(p.get('detail'), 200)
            if detail and detail != a['detail']:
                a['detail'] = detail
                changed = True
        return changed

    def _transition(self, a, sess, ev, p, is_sub):
        """이벤트 하나를 상태 변화로 옮긴다. 바뀌었으면 True."""
        state = a['state']
        if ev in ('UserPromptSubmit', 'BeforeAgent'):
            return self._set_state(a, 'working')
        if ev in ('SubagentStart',):
            return self._set_state(a, 'working')
        if ev in TOOL_EVENTS:
            # 비동기 훅은 순서가 뒤바뀔 수 있어서, 끝난(waiting/reported) 상태는 건드리지 않는다.
            if state in ('blocked', 'idle', 'error'):
                return self._set_state(a, 'working')
            return False
        if ev in ('PermissionRequest', 'Elicitation'):
            return self._set_state(a, 'blocked', clip(p.get('tool_name'), 40))
        if ev == 'Notification':
            kind = clip(p.get('notification_type'), 40)
            if kind in BLOCKING_NOTICES:
                return self._set_state(a, 'blocked', a['note'] if state == 'blocked' else '')
            if kind == 'idle_prompt' and state == 'working':
                # Stop 없이 한가해진 경우(사용자가 직접 중단): 조용히 대기 상태로.
                return self._set_state(a, 'idle')
            return False
        if ev == 'SubagentStop' or (ev == 'Stop' and is_sub):
            return self._set_state(a, 'reported')
        if ev in ('Stop', 'AfterAgent'):
            # 백그라운드 작업이 남아 있으면 "끝났다"가 아니라 아직 일하는 중이다.
            try:
                background = int(p.get('background_count') or 0)
            except (TypeError, ValueError):
                background = 0
            if background > 0:
                return self._set_state(a, 'working', 'background')
            return self._set_state(a, 'waiting')
        if ev == 'StopFailure':
            return self._set_state(a, 'error', clip(p.get('error'), 40))
        if ev == 'Interrupt':
            return self._set_state(a, 'idle')
        return False

    # ---- 그 밖의 API ---------------------------------------------------
    def set_custom(self, p, local=False):
        """훅이 없는 도구용: curl 한 줄로 타일을 직접 갱신한다.

        local=True(이 컴퓨터에서 온 요청)이면 media 에 로컬 파일 경로도 받는다.
        돌려주는 dict 의 'media_ignored' 는 받지 못한 결과물 개수다.
        """
        if not isinstance(p, dict):
            raise ValueError('body must be a JSON object')
        name = clip(p.get('id'), 80)
        state = p.get('state')
        if not name:
            raise ValueError('id is required')
        if state not in STATES:
            raise ValueError('state must be one of %s' % ', '.join(STATES))
        aid = 'custom:' + name
        with self.lock:
            a, _ = self._upsert(aid, 'custom')
            a['project'] = clip(p.get('project'), 60) or a['project']
            a['title'] = clip(p.get('title'), 60) or a['title']
            a['name'] = clip(p.get('name'), 30) or a['name']
            a['role'] = clip(p.get('role'), 40) or a['role']
            parent = 'custom:' + clip(p.get('parent'), 80) if p.get('parent') else ''
            if parent and parent != aid and parent in self.agents:
                a['parent'], a['kind'] = parent, 'subagent'
            self._set_state(a, state, clip(p.get('note'), 40))
            ignored = 0
            if self.preview:
                if 'text' in p:
                    a['text'], a['ask'] = clip(p.get('text'), 240), bool(p.get('ask'))
                if 'detail' in p:
                    a['detail'] = clip(p.get('detail'), 200)
                if 'media' in p or 'media_url' in p:
                    raw = p.get('media') if 'media' in p else p.get('media_url')
                    asked = [v for v in (raw if isinstance(raw, list) else [raw]) if isinstance(v, str) and v.strip()]
                    fresh = clean_media(asked, local)
                    ignored = len(asked) - len(fresh)
                    if fresh:
                        a['media'] = (fresh + [m for m in a['media'] if m not in fresh])[:MEDIA_MAX]
                    elif not asked:
                        a['media'] = []                 # 빈 값을 보내면 결과물을 내린다
            self._emit({'type': 'agent', 'agent': dict(a)})
            out = dict(a)
            if ignored:
                out['media_ignored'] = ignored
            return out

    def ack(self, aid=None, everyone=False):
        done = 0
        with self.lock:
            for a in list(self.agents.values()):
                if (everyone or a['id'] == aid) and a['state'] in ATTENTION and not a['acked']:
                    a['acked'] = True
                    done += 1
                    self._emit({'type': 'agent', 'agent': dict(a)})
        return done

    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError('mode must be one of %s' % ', '.join(MODES))
        with self.lock:
            self.mode = mode
            self._emit({'type': 'mode', 'mode': mode})

    def remove(self, aid):
        with self.lock:
            if aid not in self.agents:
                return False
            self._remove_tree(aid, 'removed')
            return True

    def reset(self):
        with self.lock:
            self.agents.clear()
            self._emit({'type': 'reset'})

    def sweep(self):
        """보고를 마친 서브에이전트와 오래 소식 없는 세션을 내보낸다."""
        now = time.time()
        with self.lock:
            for a in list(self.agents.values()):
                if a['id'] not in self.agents:
                    continue
                if a['state'] == 'reported' and not a['media'] and now - a['since'] > self.sub_ttl:
                    self._remove(a['id'], 'reported')       # 결과물이 걸린 타일은 다음 턴까지 둔다
                elif now - a['seen'] > self.stale_after:
                    if a['kind'] == 'session':
                        self._remove_tree(a['id'], 'stale')
                    else:
                        self._remove(a['id'], 'stale')


class Voice:
    """선택 기능: 오픈소스 Supertonic 3 로 대사를 실제 목소리로 만든다. (pip install supertonic)

    모델은 처음 한 번 Hugging Face 에서 내려받고(약 400MB), 이후에는 이 컴퓨터 안에서만 돈다.
    """
    MAX_TEXT = 200
    CACHE = 300

    def __init__(self):
        self.state = 'off'          # off | missing | loading | ready | error
        self.error = ''
        self.voices = []
        self.tts = None
        self.lock = threading.Lock()
        self.cache = OrderedDict()

    @staticmethod
    def installed():
        return importlib.util.find_spec('supertonic') is not None

    def info(self):
        return {'state': self.state, 'voices': list(self.voices), 'error': self.error}

    def start(self, on_change=None):
        if not self.installed():
            self.state = 'missing'
            return
        self.state = 'loading'

        def load():
            try:
                from supertonic import TTS
                tts = TTS(model='supertonic-3', auto_download=True)
                names = sorted(getattr(tts, 'voice_style_names', None) or [])
                if not names:
                    raise RuntimeError('no voice styles found')
                self.tts, self.voices, self.state = tts, names, 'ready'
            except Exception as e:                     # 내려받기 실패, 형식 변경 등: 옹알이로 돌아간다
                self.state, self.error = 'error', str(e)[:200]
            if on_change:
                on_change()

        threading.Thread(target=load, daemon=True).start()

    def wav(self, text, voice, speed):
        """WAV 바이트를 돌려준다. 준비 전이거나 잘못된 요청이면 ValueError."""
        if self.state != 'ready':
            raise LookupError(self.state)
        text = ' '.join(str(text or '').split())[:self.MAX_TEXT]
        if not text:
            raise ValueError('text is required')
        if voice not in self.voices:
            raise ValueError('unknown voice')
        try:
            speed = min(1.5, max(0.8, float(speed)))
        except (TypeError, ValueError):
            speed = 1.05
        key = (text, voice, round(speed, 2))
        with self.lock:
            hit = self.cache.get(key)
            if hit is not None:
                self.cache.move_to_end(key)
                return hit
            style = self.tts.get_voice_style(voice)
            samples, _ = self.tts.synthesize(text, voice_style=style, speed=speed, lang='ko' if any(
                '\uac00' <= ch <= '\ud7a3' for ch in text) else 'en')
            data = encode_wav(samples, self.tts.sample_rate)
            self.cache[key] = data
            while len(self.cache) > self.CACHE:
                self.cache.popitem(last=False)
            return data


def encode_wav(samples, rate):
    """float 파형(-1~1)을 16비트 모노 WAV 로."""
    import numpy as np
    pcm = (np.clip(np.asarray(samples, dtype=np.float32).reshape(-1), -1.0, 1.0) * 32767).astype('<i2')
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


STORE = Store()
VOICE = Voice()
CFG = {'token': '', 'quiet': False,
       'results_dir': os.environ.get('AGENT_MONITOR_RESULTS', os.path.join('~', 'Downloads', 'agent-monitor'))}


def log(msg):
    if not CFG['quiet']:
        print(time.strftime('%H:%M:%S'), msg, flush=True)


def is_loopback(ip):
    try:
        addr = ipaddress.ip_address(ip.split('%')[0])
    except ValueError:
        return False
    if getattr(addr, 'ipv4_mapped', None):
        addr = addr.ipv4_mapped
    return addr.is_loopback


def host_allowed(host_header):
    """DNS 리바인딩 방지: Host 는 IP 주소, localhost, *.local 만 받는다."""
    if not host_header:
        return False
    host = host_header.strip()
    if host.startswith('['):
        host = host[1:].split(']')[0]
    elif host.count(':') == 1:
        host = host.rsplit(':', 1)[0]
    host = host.lower().rstrip('.')
    if host == 'localhost' or host.endswith('.local'):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = 'AgentMonitor/0.1'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        pass

    # ---- 응답 도우미 ----------------------------------------------------
    def _json(self, code, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _token(self, query):
        tok = (query.get('token') or [''])[0]
        if tok:
            return tok
        auth = self.headers.get('Authorization') or ''
        if auth.lower().startswith('bearer '):
            return auth[7:].strip()
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie') or '')
        except Exception:
            return ''
        return cookie['am_token'].value if 'am_token' in cookie else ''

    def _guard(self, query):
        """요청을 받아도 되는지 검사한다. 안 되면 응답을 보내고 False."""
        if not host_allowed(self.headers.get('Host')):
            self.close_connection = True
            self._json(403, {'ok': False, 'error': 'host not allowed'})
            return False
        if is_loopback(self.client_address[0]):
            return True
        tok = self._token(query)
        if CFG['token'] and tok and hmac.compare_digest(tok.encode(), CFG['token'].encode()):
            return True
        self.close_connection = True
        self._json(401, {'ok': False, 'error': 'token required'})
        return False

    def _body(self):
        ctype = (self.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if ctype != 'application/json':
            raise ValueError('Content-Type must be application/json')
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            raise ValueError('bad Content-Length')
        if n > MAX_BODY:
            raise ValueError('body too large')
        raw = self.rfile.read(n) if n else b'{}'
        try:
            return json.loads(raw.decode('utf-8') or '{}')
        except Exception:
            raise ValueError('body must be JSON')

    # ---- 라우팅 ---------------------------------------------------------
    def do_POST(self):
        u = urlparse(self.path)
        query = parse_qs(u.query)
        if not self._guard(query):
            return
        path = u.path
        local = is_loopback(self.client_address[0])       # 로컬 파일 경로는 이 컴퓨터에서 온 요청만
        try:
            body = self._body()
            if path.startswith('/hook/'):
                source = path[len('/hook/'):]
                STORE.apply_hook(source, body, local=local)
                log('[%s] %s %s%s' % (source, clip(body.get('hook_event_name'), 30), clip(body.get('session_id'), 12),
                                      ' (auto)' if body.get('auto_prompt') else ''))
                return self._json(200, {'ok': True})
            if path == '/api/ack':
                n = STORE.ack(clip(body.get('id'), 300), bool(body.get('all')))
                return self._json(200, {'ok': True, 'acked': n})
            if path == '/api/mode':
                STORE.set_mode(body.get('mode'))
                log('mode → %s' % body.get('mode'))
                return self._json(200, {'ok': True, 'mode': STORE.mode})
            if path == '/api/agent':
                return self._json(200, {'ok': True, 'agent': STORE.set_custom(body, local=local)})
            if path == '/api/remove':
                return self._json(200, {'ok': STORE.remove(clip(body.get('id'), 300))})
            if path == '/api/reveal':
                if not local:
                    return self._json(403, {'ok': False, 'error': 'only on this computer'})
                try:
                    target = result_file(body.get('url'))
                except LookupError as e:
                    return self._json(404, {'ok': False, 'error': str(e)})
                except Exception as e:
                    return self._json(502, {'ok': False, 'error': str(e)[:200]})
                reveal(target)
                return self._json(200, {'ok': True, 'path': short_path(target)})
            if path == '/api/reset':
                STORE.reset()
                return self._json(200, {'ok': True})
            return self._json(404, {'ok': False, 'error': 'not found'})
        except ValueError as e:
            self.close_connection = True        # 본문을 다 읽지 않았을 수 있다
            return self._json(400, {'ok': False, 'error': str(e)})

    def do_GET(self):
        u = urlparse(self.path)
        query = parse_qs(u.query)
        if not self._guard(query):
            return
        path = u.path
        if path == '/api/state':
            return self._json(200, {'ok': True, 'agents': STORE.snapshot(), 'mode': STORE.mode,
                                    'now': time.time(), 'voice': VOICE.info()})
        if path == '/api/tts':
            return self._tts(query)
        if path == '/api/client':
            return self._json(200, {'ok': True, 'local': is_loopback(self.client_address[0]),
                                    'results_dir': short_path(results_dir())})
        if path == '/api/download':
            return self._download(query)
        if path == '/api/health':
            return self._json(200, {'ok': True})
        if path == '/events':
            return self._sse()
        if path.startswith('/media/'):
            return self._media(path)
        if path == '/':
            path = '/index.html'
        target = os.path.realpath(os.path.join(WEB, path.lstrip('/')))
        if os.path.commonpath([WEB, target]) != WEB or not os.path.isfile(target):
            return self._json(404, {'ok': False, 'error': 'not found'})
        return self._file(target, query)

    def do_HEAD(self):
        u = urlparse(self.path)
        if not self._guard(parse_qs(u.query)):
            return
        if u.path.startswith('/media/'):
            return self._media(u.path, head=True)
        self._json(405, {'ok': False, 'error': 'method not allowed'})

    def _media(self, path, head=False):
        """결과물 파일. 아이패드 사파리가 영상을 틀려면 Range(부분 요청)를 지원해야 한다."""
        real = MEDIA.lookup(path)
        if not real:
            return self._json(404, {'ok': False, 'error': 'not found'})
        return self._send_file(real, head=head)

    def _download(self, query):
        """화면에 뜬 결과물을 파일로 내려받기. 인터넷 주소면 결과물 폴더에도 저장해 둔다."""
        url = (query.get('url') or [''])[0]
        try:
            target = result_file(url)
        except LookupError as e:
            return self._json(404, {'ok': False, 'error': str(e)})
        except Exception as e:
            return self._json(502, {'ok': False, 'error': str(e)[:200]})
        return self._send_file(target, attachment=os.path.basename(target))

    def _send_file(self, real, head=False, attachment=''):
        try:
            fh = open(real, 'rb')
        except OSError:
            return self._json(404, {'ok': False, 'error': 'not found'})
        with fh:
            size = os.fstat(fh.fileno()).st_size
            start, end, code = 0, size - 1, 200
            m = re.match(r'^bytes=(\d*)-(\d*)$', (self.headers.get('Range') or '').strip())
            if m and (m.group(1) or m.group(2)) and not attachment:
                if m.group(1):
                    start = int(m.group(1))
                    end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:                                       # bytes=-500 : 끝에서 500바이트
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', 'bytes */%d' % size)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                code = 206
            length = end - start + 1
            self.send_response(code)
            self.send_header('Content-Type', MEDIA_TYPES.get(os.path.splitext(real)[1].lower(), 'application/octet-stream'))
            self.send_header('Content-Length', str(length))
            self.send_header('Accept-Ranges', 'bytes')
            if code == 206:
                self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
            if attachment:
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''%s" % quote(attachment))
            self.send_header('Cache-Control', 'private, max-age=3600')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            if head:
                return
            fh.seek(start)
            left = length
            try:
                while left > 0:
                    chunk = fh.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

    def _tts(self, query):
        arg = lambda k: (query.get(k) or [''])[0]
        try:
            data = VOICE.wav(arg('text'), arg('voice'), arg('speed') or 1.05)
        except LookupError as e:
            return self._json(503, {'ok': False, 'error': 'voice ' + str(e), 'voice': VOICE.info()})
        except ValueError as e:
            return self._json(400, {'ok': False, 'error': str(e)})
        except Exception as e:
            return self._json(500, {'ok': False, 'error': str(e)[:200]})
        self.send_response(200)
        self.send_header('Content-Type', 'audio/wav')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'private, max-age=3600')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path, query):
        with open(path, 'rb') as fh:
            body = fh.read()
        ctype = mimetypes.guess_type(path)[0] or 'application/octet-stream'
        if ctype.startswith('text/') or ctype == 'application/javascript':
            ctype += '; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        tok = (query.get('token') or [''])[0]
        if tok and CFG['token'] and hmac.compare_digest(tok.encode(), CFG['token'].encode()):
            # 주소의 토큰을 쿠키로 옮겨서, 이후 /events·/api 요청에 자동으로 붙게 한다.
            self.send_header('Set-Cookie', 'am_token=%s; Path=/; HttpOnly; SameSite=Strict' % tok)
        self.end_headers()
        self.wfile.write(body)

    def _sse(self):
        q, first = STORE.subscribe()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'close')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(('retry: 2000\ndata: %s\n\n' % first).encode())
            self.wfile.flush()
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(('data: %s\n\n' % data).encode())
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            STORE.unsubscribe(q)


def voice_line():
    s = VOICE.state
    if s == 'loading':
        return 'AI 음성: 준비 중 (처음이면 모델을 내려받습니다, 약 400MB)'
    if s == 'ready':
        return 'AI 음성: 준비됨 (%s)' % ', '.join(VOICE.voices)
    if s == 'error':
        return 'AI 음성: 실패, 옹알이로 대신합니다 (%s)' % VOICE.error
    if s == 'missing':
        return 'AI 음성: 꺼짐. 켜려면 python3 start.py --install-voice'
    return 'AI 음성: 꺼짐'


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


def sweeper(stop):
    while not stop.wait(2.0):
        STORE.sweep()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # 브라우저가 먼저 연결을 끊는 일(새로고침, 영상 끊어 받기, 아이패드 잠금)은 정상이다. 조용히 넘긴다.
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_server(host, port):
    return Server((host, port), Handler)


def build_parser(description='AI 에이전트 세션을 화상회의 화면으로 보여주는 모니터'):
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument('--port', type=int, default=int(os.environ.get('AGENT_MONITOR_PORT', 8790)))
    ap.add_argument('--lan', action='store_true', help='같은 네트워크의 다른 기기에서도 접속 (토큰 필요)')
    ap.add_argument('--token', default=os.environ.get('AGENT_MONITOR_TOKEN', ''),
                    help='--lan 접속용 토큰 (생략하면 실행할 때마다 새로 만든다)')
    ap.add_argument('--mode', choices=MODES, default='nag', help='시작 모드')
    ap.add_argument('--sub-ttl', type=float, default=20.0, help='보고를 마친 서브에이전트가 남아 있는 시간(초)')
    ap.add_argument('--quiet', action='store_true', help='이벤트 로그를 찍지 않는다')
    ap.add_argument('--open', action='store_true', help='켜지면 브라우저로 화면을 연다')
    ap.add_argument('--no-preview', action='store_true',
                    help='응답 미리보기·명령·결과물 주소를 받지도 보여주지도 않는다')
    ap.add_argument('--no-voice', action='store_true', help='AI 음성(Supertonic)이 설치돼 있어도 쓰지 않는다')
    ap.add_argument('--results-dir', default=CFG['results_dir'],
                    help='화면에서 저장·폴더 열기를 누른 결과물을 모아 둘 폴더 (기본 ~/Downloads/agent-monitor)')
    return ap


def run(a):
    STORE.mode = a.mode
    STORE.sub_ttl = a.sub_ttl
    STORE.preview = not a.no_preview
    CFG['quiet'] = a.quiet
    CFG['results_dir'] = a.results_dir
    host = '127.0.0.1'
    if a.lan:
        host = '0.0.0.0'
        CFG['token'] = a.token or secrets.token_urlsafe(9)
    try:
        srv = make_server(host, a.port)
    except OSError as e:
        print('포트 %d 을(를) 열 수 없습니다: %s' % (a.port, e.strerror or e))
        print('이미 켜져 있다면 http://localhost:%d/ 를 여세요. 다른 포트로 켜려면 --port 를 쓰세요.' % a.port)
        sys.exit(1)

    stop = threading.Event()
    threading.Thread(target=sweeper, args=(stop,), daemon=True).start()
    if not a.no_voice:
        VOICE.start(on_change=lambda: (STORE.voice_changed(), print(voice_line(), flush=True)))

    print('=' * 62)
    print(' agent-monitor 실행 중')
    print(' 이 컴퓨터에서 열기:   http://localhost:%d/' % a.port)
    print(' 데모(훅 없이 구경):   http://localhost:%d/?demo=1' % a.port)
    if a.lan:
        print(' 다른 기기에서 열기:   http://%s:%d/?token=%s' % (lan_ip(), a.port, CFG['token']))
        print(' (토큰이 든 주소는 같은 와이파이의 내 기기에서만 여세요)')
    else:
        print(' 아이패드 등에서 보려면 --lan 옵션으로 다시 실행하세요')
    print(' 결과물 폴더:          %s (화면의 저장·폴더 버튼)' % short_path(results_dir()))
    print(' ' + voice_line())
    print(' 끄기: Ctrl+C')
    print('=' * 62, flush=True)
    if a.open:
        threading.Timer(0.4, webbrowser.open, ['http://localhost:%d/' % a.port]).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n종료')
    finally:
        stop.set()


def main(argv=None):
    run(build_parser().parse_args(argv))


if __name__ == '__main__':
    main()
