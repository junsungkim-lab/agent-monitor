#!/usr/bin/env python3
"""호스트 릴스 하나를 결과물로 내보낸다. (agent-monitor 데모용, 표준 라이브러리만 사용)

    python3 make_reel.py 3 --hello  # 3번 호스트 이름표만 바로 붙인다 (일을 시작할 때)
    python3 make_reel.py 3          # 3번 호스트 릴스를 out/ 에 내보내고 경로를 출력
    python3 make_reel.py --lead     # 총괄(PD) 타일 이름표만 붙인다
    python3 make_reel.py --list     # 호스트 목록

재현 모드다. 영상을 새로 생성하지 않고, 미리 만든 편집본을 결과물로 내보낸다.
- media/<번호>_<이름>.mp4 를 결과물로 쓴다. 호스트에 "remote" 주소를 적어 두면 파일이 없을 때 거기서 내려받는다.
- 둘 다 없으면 결과물 없이 끝난다. (모니터의 이름표·결과 한 줄은 그대로 뜬다)
- 실제 생성에 걸리는 시간 대신 호스트마다 정해 둔 시간(seconds)만큼 기다린다. 끝나는 순서가
  엇갈려야 모니터에서 한 명씩 보고하는 모습이 보인다. REEL_SPEED=4 처럼 주면 그만큼 빨라진다.

'[agent-monitor] name: ...' / 'text: ...' 줄은 agent-monitor 훅이 읽어서 이 작업을 하는 에이전트
타일의 이름표와 결과 한 줄로 붙인다. 마지막의 '완성: <경로>' 는 훅이 결과물로 띄운다.
"""
import json
import os
import shutil
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def load():
    with open(os.path.join(HERE, 'hosts.json'), encoding='utf-8') as f:
        return json.load(f)


def boxes(data, start, end):
    while start + 8 <= end:
        size = int.from_bytes(data[start:start + 4], 'big')
        kind = data[start + 4:start + 8]
        head = 8
        if size == 1:
            size, head = int.from_bytes(data[start + 8:start + 16], 'big'), 16
        elif size == 0:
            size = end - start
        if size < head:
            return
        yield kind, start + head, min(start + size, end)
        start += size


def probe(path):
    """mp4 머리말에서 길이(초)와 화면 크기를 읽는다. 못 읽으면 None."""
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return None
    seconds, width, height = None, 0, 0
    for kind, a, b in boxes(data, 0, len(data)):
        if kind != b'moov':
            continue
        for k2, a2, b2 in boxes(data, a, b):
            if k2 == b'mvhd':
                v = data[a2]
                if v == 1:
                    scale, dur = int.from_bytes(data[a2 + 20:a2 + 24], 'big'), int.from_bytes(data[a2 + 24:a2 + 32], 'big')
                else:
                    scale, dur = int.from_bytes(data[a2 + 12:a2 + 16], 'big'), int.from_bytes(data[a2 + 16:a2 + 20], 'big')
                if scale:
                    seconds = dur / scale
            elif k2 == b'trak':
                for k3, a3, b3 in boxes(data, a2, b2):
                    if k3 == b'tkhd' and b3 - a3 >= 84:
                        w, h = int.from_bytes(data[b3 - 8:b3 - 4], 'big') >> 16, int.from_bytes(data[b3 - 4:b3], 'big') >> 16
                        if w * h > width * height:
                            width, height = w, h
    return seconds, width, height


def wait(seconds):
    speed = max(0.1, float(os.environ.get('REEL_SPEED') or 1))
    total = seconds / speed
    steps = 5
    for i in range(1, steps + 1):
        time.sleep(total / steps)
        print('진행 %d%%' % (i * 100 // steps), flush=True)


def fetch(url, dest):
    tmp = dest + '.part'
    opener = urllib.request.build_opener()
    with opener.open(url, timeout=60) as r, open(tmp, 'wb') as f:
        shutil.copyfileobj(r, f)
    os.replace(tmp, dest)


def deliver(cfg, host):
    name = '%d_%s.mp4' % (host['n'], host['slug'])
    print('[agent-monitor] name: %s' % host['name'])
    print('[agent-monitor] role: %s' % cfg.get('role', 'host'))
    print('%s · %s 릴스 준비 중 (재현 모드: 미리 만든 편집본을 씀)' % (host['name'], host['topic']), flush=True)
    out_dir = os.path.join(HERE, 'out')
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, name)
    local = os.path.join(HERE, 'media', name)
    wait(float(host.get('seconds') or 10))
    if os.path.isfile(local):
        shutil.copyfile(local, dest)
    elif host.get('remote'):
        print('media/%s 가 없어 내려받습니다' % name, flush=True)
        fetch(host['remote'], dest)
    else:
        print('[agent-monitor] text: %s 릴스 · 영상 파일 없음 (media/%s)' % (host['name'], name))
        print('media/%s 에 영상을 넣으면 결과물로 뜹니다.' % name)
        return 0
    info = probe(dest)
    size = os.path.getsize(dest) / 1e6
    if info and info[0]:
        spec = '%.1f초 · %d×%d' % (info[0], info[1], info[2])
        print('길이 %s · %.1fMB' % (spec, size))
    else:
        spec = '%.1fMB' % size
        print(spec)
    print('[agent-monitor] text: %s 릴스 완성 · %s' % (host['name'], spec))
    print('완성: %s' % dest)
    return 0


def main(argv):
    cfg = load()
    if not argv or argv[0] in ('-h', '--help'):
        print(__doc__)
        return 0
    if argv[0] == '--lead':
        print('[agent-monitor] name: %s' % cfg.get('lead', 'PD'))
        print('호스트 %d명에게 릴스를 하나씩 맡깁니다.' % len(cfg['hosts']))
        return 0
    if argv[0] == '--list':
        for h in cfg['hosts']:
            print('%d  %s  (%s)' % (h['n'], h['name'], h['topic']))
        return 0
    try:
        n = int(argv[0])
        host = next(h for h in cfg['hosts'] if h['n'] == n)
    except (ValueError, StopIteration):
        print('호스트 번호를 주세요: 1~%d' % len(cfg['hosts']))
        return 2
    if '--hello' in argv[1:]:
        print('[agent-monitor] name: %s' % host['name'])
        print('[agent-monitor] role: %s' % cfg.get('role', 'host'))
        print('%s · %s 맡았습니다.' % (host['name'], host['topic']))
        return 0
    return deliver(cfg, host)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
