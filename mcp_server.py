#!/usr/bin/env python3
"""agent-monitor MCP 서버 (stdio, Python 3 표준 라이브러리만 사용)

훅을 쓸 수 없는 환경(예: Claude 데스크톱 앱)에서, 에이전트가 도구 호출로 직접
자기 진행 상황과 결과물을 모니터에 올릴 수 있게 한다.

    Claude Code:    claude mcp add agent-monitor --scope user -- python3 /경로/agent-monitor/mcp_server.py
    Claude Desktop: claude_desktop_config.json 의 mcpServers 에 아래를 추가
        "agent-monitor": {"command": "/usr/bin/python3", "args": ["/경로/agent-monitor/mcp_server.py"]}

모니터 주소는 AGENT_MONITOR_URL 환경변수로 바꾼다. (기본 http://127.0.0.1:8790)
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get('AGENT_MONITOR_URL', 'http://127.0.0.1:8790').rstrip('/')
TOKEN = os.environ.get('AGENT_MONITOR_TOKEN', '')
SERVER_INFO = {'name': 'agent-monitor', 'version': '0.1.0'}
STATES = ['idle', 'working', 'waiting', 'blocked', 'error', 'reported']

REPORT_PROPS = {
    'id': {'type': 'string', 'description': 'Stable id for this worker tile, e.g. "host-1". Reuse the same id for every update.'},
    'state': {'type': 'string', 'enum': STATES,
              'description': 'working=in progress; waiting=finished or asking the user, needs their attention; '
                             'blocked=cannot continue without approval/input; error=failed; '
                             'reported=a sub-worker finished and handed its result back; idle=nothing to do'},
    'name': {'type': 'string', 'description': 'Display name on the tile, e.g. "재테크 민지". Optional.'},
    'role': {'type': 'string', 'description': 'Short role label for a sub-worker, e.g. "host video". Optional.'},
    'parent': {'type': 'string', 'description': 'id of the lead tile this worker belongs to. Makes it a sub-worker tile. Optional.'},
    'project': {'type': 'string', 'description': 'Short context label under the name (topic or project). Optional.'},
    'text': {'type': 'string', 'description': 'One or two short lines shown on the tile: the result summary, or the question you are asking.'},
    'ask': {'type': 'boolean', 'description': 'true when `text` is a question the user must answer.'},
    'detail': {'type': 'string', 'description': 'For state=blocked: the command or action waiting for approval.'},
    'media_url': {'type': 'string', 'description': 'The result image or video: an http(s) URL exactly as the generating tool returned it, '
                                                   'or the absolute path of a file on this computer (mp4, mov, webm, png, jpg, webp, gif). '
                                                   'The tile announces it briefly, then plays it.'},
}

INSTRUCTIONS = (
    'The user keeps a live agent monitor open next to their work. Use monitor_report to keep it current: '
    'set a tile to working when you start a task, and when an image or video generation finishes '
    '(Higgsfield or any other tool), report state=waiting with a one-line text and media_url set to the '
    'result URL exactly as the tool returned it, or the absolute path if you saved it to a file. '
    'For several parallel workers, give each its own id and set parent to the lead tile id. '
    'Report questions with ask=true and anything waiting for approval with state=blocked.')

TOOLS = [
    {'name': 'monitor_report',
     'description': 'Create or update one tile on the live agent monitor. Call it whenever a worker starts, finishes, '
                    'asks a question, gets blocked, fails, or produces a result such as a generated image or video '
                    '(for example a Higgsfield result URL). Only the fields you pass are changed. '
                    'Never invent ids, URLs or paths: pass media_url exactly as the tool that produced it returned it.',
     'inputSchema': {'type': 'object', 'properties': REPORT_PROPS, 'required': ['id', 'state']}},
    {'name': 'monitor_remove', 'description': 'Remove a tile (and its sub-worker tiles) from the monitor. '
                                              'Use the id you reported, or the full id from monitor_state for tiles made by CLI hooks.',
     'inputSchema': {'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id']}},
    {'name': 'monitor_state', 'description': 'Read every tile currently on the monitor, including whether the user marked it as seen (acked).',
     'inputSchema': {'type': 'object', 'properties': {}}},
    {'name': 'monitor_reset', 'description': 'Clear all tiles. Use before a new run, only when the user wants a clean screen.',
     'inputSchema': {'type': 'object', 'properties': {}}},
]

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(method, path, body=None):
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    headers = {'Content-Type': 'application/json'}
    if TOKEN:
        headers['Authorization'] = 'Bearer ' + TOKEN
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    with OPENER.open(req, timeout=5) as r:
        return json.loads(r.read().decode())


def call_tool(name, args):
    try:
        if name == 'monitor_report':
            body = {k: v for k, v in args.items() if k in REPORT_PROPS and v is not None}
            if isinstance(body.get('media_url'), str) and body['media_url'].startswith('~'):
                body['media_url'] = os.path.expanduser(body['media_url'])
            a = http('POST', '/api/agent', body)['agent']
            shown = ' (result shown)' if a.get('media') else ''
            if a.get('media_ignored'):
                shown += ' (media_url not shown: use an http(s) URL or the absolute path of an existing image/video file)'
            return 'ok: %s → %s%s' % (args.get('id'), a.get('state'), shown), False
        if name == 'monitor_remove':
            tid = str(args.get('id') or '')
            # 직접 올린 타일은 'host-1' 처럼, 훅이 만든 타일은 monitor_state 의 id 그대로('claude:...')
            ok = http('POST', '/api/remove', {'id': tid if ':' in tid else 'custom:' + tid})['ok']
            return ('ok: removed' if ok else 'no such tile'), False
        if name == 'monitor_state':
            return json.dumps(http('GET', '/api/state')['agents'], ensure_ascii=False, indent=1), False
        if name == 'monitor_reset':
            http('POST', '/api/reset', {})
            return 'ok: monitor cleared', False
        return 'unknown tool ' + str(name), True
    except urllib.error.HTTPError as e:
        return 'monitor error %s: %s' % (e.code, e.read().decode(errors='replace')), True
    except Exception as e:
        return ('monitor not reachable at %s (%s). The work itself can continue; ask the user to run '
                '`python3 start.py` in the agent-monitor folder.' % (BASE, e)), True


def send(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        mid, method = msg.get('id'), msg.get('method')
        if mid is None:
            continue                                   # 알림(notifications/*)에는 답하지 않는다
        if method == 'initialize':
            version = (msg.get('params') or {}).get('protocolVersion') or '2025-06-18'
            send({'jsonrpc': '2.0', 'id': mid, 'result': {
                'protocolVersion': version, 'capabilities': {'tools': {'listChanged': False}},
                'serverInfo': SERVER_INFO,
                'instructions': INSTRUCTIONS}})
        elif method == 'tools/list':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {'tools': TOOLS}})
        elif method == 'tools/call':
            params = msg.get('params') or {}
            text, is_error = call_tool(params.get('name'), params.get('arguments') or {})
            send({'jsonrpc': '2.0', 'id': mid, 'result': {'content': [{'type': 'text', 'text': text}], 'isError': is_error}})
        elif method == 'ping':
            send({'jsonrpc': '2.0', 'id': mid, 'result': {}})
        elif method in ('resources/list', 'prompts/list'):
            send({'jsonrpc': '2.0', 'id': mid, 'result': {method.split('/')[0]: []}})
        else:
            send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'Method not found: %s' % method}})


if __name__ == '__main__':
    main()
