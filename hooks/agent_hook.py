#!/usr/bin/env python3
"""agent-monitor 훅: CLI가 넘겨준 이벤트를 모니터 서버로 전달한다.

    python3 agent_hook.py claude|codex|gemini [--url http://127.0.0.1:8790] [--no-preview]
    (이벤트 JSON은 stdin으로 들어온다. 주소는 AGENT_MONITOR_URL 환경변수로도 바꿀 수 있다.)

보내는 것:
- 항상: 이벤트 이름, 세션/서브에이전트 ID와 타입, 알림 종류, 도구 이름, 작업 폴더의 마지막 이름.
- 도구가 '[agent-monitor] name: 이름' 줄을 출력했으면 그 이름(타일 이름표).
- 미리보기(기본 켬, --no-preview 로 끔): 마지막 응답의 앞/뒤 몇 줄, 승인을 기다리는 명령 한 줄,
  도구 결과나 마지막 응답에 나온 이미지·영상 결과물(인터넷 주소, 또는 이 컴퓨터에 실제로 있는
  파일의 경로). 토큰처럼 보이는 문자열은 가리고 보낸다.
보내지 않는 것: 프롬프트, 응답 전문, 도구 입출력 전문, 작업 폴더 전체 경로, 트랜스크립트 경로.
결과물 파일 경로는 이 컴퓨터의 서버에만 가고, 화면에는 경로 대신 임의의 주소로 나간다.

stdout 에는 아무것도 쓰지 않는다. (CLI가 훅 출력을 대화에 끼워 넣을 수 있다.)
서버가 꺼져 있거나 느려도 CLI 작업을 막지 않는다. 항상 0으로 끝난다.
"""
import json
import os
import re
import sys
import urllib.request

KEEP = ('hook_event_name', 'session_id', 'agent_id', 'agent_type', 'notification_type',
        'source', 'reason', 'tool_name', 'session_title', 'error')
SOURCES = ('claude', 'codex', 'gemini')
PREVIEW = 240
DETAIL_KEYS = ('command', 'file_path', 'path', 'url', 'query', 'pattern', 'description')

# KEY=값, "key": "값", --token 값, Bearer 값 꼴만 가린다. 문장 속의 단어(token, password)는 건드리지 않는다.
SECRET_PAIR = re.compile(
    r'(?i)((?:authorization|[\w-]*token|[\w-]*secret|password|passwd|pwd|[\w-]*api[_-]?key|access[_-]?key|private[_-]?key)'
    r'["\']?\s*[:=]\s*["\']?(?:(?:bearer|basic|token)\s+)?|\bbearer\s+|--(?:token|password|secret|api-key)\s+)([^\s"\']+)')
SECRET_BLOB = re.compile(r'[A-Za-z0-9_\-+=]{32,}')
MEDIA_EXT = r'(?:mp4|webm|mov|m4v|png|jpe?g|webp|gif)'
MEDIA_URL = re.compile(
    r'https?://[^\s"\'<>\\)\]]+?\.' + MEDIA_EXT + r'(?:\?[^\s"\'<>\\)\]]*)?', re.I)
# 이 컴퓨터의 결과물 파일 경로: /절대, ~/홈, ./상대, 또는 폴더/파일 꼴 (슬래시가 하나는 있어야 한다)
LOCAL_MEDIA = re.compile(
    r'(?<![A-Za-z0-9_/.~\-])((?:~|\.{1,2})?/?(?:[\w.\-]+/)+[\w.\-]+\.' + MEDIA_EXT + r')(?![A-Za-z0-9_/])', re.I)
# 도구가 자기 타일에 이름표·결과 한 줄을 붙일 때 출력하는 줄:  [agent-monitor] name: 재테크 민지
LABEL = re.compile(r'^\s*\[agent-monitor\]\s*(name|role|text)\s*[:=]\s*(.+?)\s*$', re.I | re.M)
MEDIA_FILE_MAX = 4 * 1024 ** 3
MARKUP = re.compile(r'[*_`#>|]+')


def mask(text):
    """화면에 나가기 전에 비밀값처럼 보이는 것을 가린다. (완벽하지 않다)"""
    text = SECRET_PAIR.sub(lambda m: m.group(1) + '***', text)
    text = SECRET_BLOB.sub('***', text)
    home = os.path.expanduser('~')
    if len(home) > 1:
        text = text.replace(home, '~')
    return text


def lines_of(text):
    out = []
    for line in text.splitlines():
        line = ' '.join(MARKUP.sub('', line).split())
        if line and not set(line) <= set('-=. '):
            out.append(line)
    return out


def head_of(text):
    acc = ''
    for line in lines_of(text):
        acc = (acc + ' ' + line).strip()
        if len(acc) >= PREVIEW:
            break
    return mask(acc)[:PREVIEW]


def tail_of(text):
    acc = ''
    for line in reversed(lines_of(text)):
        nxt = (line + ' ' + acc).strip()
        if acc and len(nxt) > PREVIEW:
            break
        acc = nxt
    return mask(acc)[-PREVIEW:]


def detail_of(payload):
    tool_input = payload.get('tool_input')
    if isinstance(tool_input, dict):
        for key in DETAIL_KEYS:
            value = tool_input.get(key)
            if isinstance(value, list):
                value = ' '.join(str(v) for v in value)
            if isinstance(value, str) and value.strip():
                return mask(' '.join(value.split()))[:200]
    message = payload.get('message')
    if isinstance(message, str) and message.strip():
        return mask(' '.join(message.split()))[:200]
    return ''


def strings_of(value, budget=400000):
    """도구 결과(JSON)에 든 문자열을 줄바꿈을 살린 채로 모은다."""
    out, stack, used = [], [value], 0
    while stack and used < budget:
        v = stack.pop()
        if isinstance(v, str):
            out.append(v[:budget - used])
            used += len(v)
        elif isinstance(v, dict):
            stack.extend(reversed(list(v.values())))
        elif isinstance(v, list):
            stack.extend(reversed(v))
    return out


def local_media(text, cwd):
    """글 속의 결과물 파일 경로 가운데 실제로 있는 것만, 절대 경로로."""
    found, candidates = [], []
    for token in text.split():                      # 경로는 공백 없는 한 덩어리. 큰 출력에서도 빠르게.
        if len(token) > 1024 or '/' not in token or '.' not in token or '://' in token:
            continue
        candidates.extend(LOCAL_MEDIA.findall(token))
        if len(candidates) > 40:
            break
    for raw in candidates:
        path = os.path.expanduser(raw)
        if not os.path.isabs(path):
            if not cwd:
                continue
            path = os.path.join(cwd, path)
        path = os.path.realpath(path)
        try:
            if os.path.isfile(path) and 0 < os.path.getsize(path) <= MEDIA_FILE_MAX and path not in found:
                found.append(path)
        except OSError:
            pass
    return found


def media_of(texts, cwd=''):
    """이미지·영상 결과물: 인터넷 주소와, 이 컴퓨터에 실제로 있는 파일 경로."""
    found = []
    for text in texts:
        for url in MEDIA_URL.findall(text):
            if url not in found and len(url) <= 2000:
                found.append(url)
        for path in local_media(text, cwd):
            if path not in found:
                found.append(path)
    return found[:4]


def label_of(texts):
    label = {}
    for text in texts:
        for key, value in LABEL.findall(text[:20000]):
            key = key.lower()
            value = ' '.join(value.split())
            label[key] = mask(value)[:240] if key == 'text' else value[:40]
    return label


def build(payload, preview=True):
    out = {}
    for key in KEEP:
        value = payload.get(key)
        if isinstance(value, str) and value:
            out[key] = value[:200]
    cwd = payload.get('cwd')
    cwd = cwd if isinstance(cwd, str) else ''
    if cwd:
        out['project'] = os.path.basename(cwd.rstrip('/\\'))[:60]
    prompt = payload.get('prompt')
    if isinstance(prompt, str) and prompt.lstrip().startswith('<'):
        # 사람이 친 말이 아니라 CLI 가 넣은 알림(<task-notification> 등). 내용은 보내지 않는다.
        out['auto_prompt'] = True
    tasks = payload.get('background_tasks')
    if isinstance(tasks, list):
        out['background_count'] = len(tasks)
    response = strings_of(payload.get('tool_response')) if payload.get('tool_response') is not None else []
    label = label_of(response)
    if label:
        out['label'] = label
    if not preview:
        return out
    said = payload.get('last_assistant_message') or payload.get('prompt_response')
    said = said if isinstance(said, str) and said.strip() else ''
    # 결과물 경로만 있는 줄은 요약에서 뺀다. (그 파일은 타일에 영상·이미지로 뜬다)
    summary = '\n'.join(line for line in said.splitlines() if not LOCAL_MEDIA.fullmatch(line.strip(' `*-:')))
    if summary.strip():
        out['head'] = head_of(summary)
        out['tail'] = tail_of(summary)
    detail = detail_of(payload)
    if detail:
        out['detail'] = detail
    media = media_of(response + ([said] if said else []), cwd)
    if media:
        out['media'] = media
    return out


def main():
    args = sys.argv[1:]
    source = args[0] if args else 'claude'
    if source not in SOURCES:
        return
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    data = json.dumps(build(payload, preview='--no-preview' not in args)).encode()
    base = os.environ.get('AGENT_MONITOR_URL', 'http://127.0.0.1:8790')
    if '--url' in args[:-1]:                    # 환경변수를 넘겨주지 않는 CLI용
        base = args[args.index('--url') + 1]
    base = base.rstrip('/')
    headers = {'Content-Type': 'application/json'}
    token = os.environ.get('AGENT_MONITOR_TOKEN')
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(base + '/hook/' + source, data=data, method='POST', headers=headers)
    # 환경변수의 프록시 설정은 무시한다. (localhost 요청이 프록시로 새지 않게)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(req, timeout=0.8).read()
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
