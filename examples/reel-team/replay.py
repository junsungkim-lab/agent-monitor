#!/usr/bin/env python3
"""에이전트 없이 릴스 팀 흐름을 모니터에 재생한다. (agent-monitor 데모용, 표준 라이브러리만 사용)

    python3 replay.py              # 켜져 있는 모니터(http://127.0.0.1:8790)에 재생
    python3 replay.py --speed 3    # 3배 빠르게
    python3 replay.py --keep       # 화면을 비우지 않고 이어서

Claude Code 로 실제 서브에이전트를 돌리는 쪽은 README 의 /reels 를 쓴다.
이 스크립트는 같은 장면을 모니터 API(/api/agent)로 직접 올린다. 리허설이나 Claude Code 가 없을 때 쓴다.
결과물은 media/ 의 파일을 쓰고, 없으면 hosts.json 의 "remote" 주소를, 그것도 없으면 결과물 없이 보고만 한다.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def post(base, path, body):
    headers = {'Content-Type': 'application/json'}
    token = os.environ.get('AGENT_MONITOR_TOKEN')
    if token:
        headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(base + path, data=json.dumps(body, ensure_ascii=False).encode(),
                                 method='POST', headers=headers)
    with OPENER.open(req, timeout=5) as r:
        return json.loads(r.read().decode())


def main(argv=None):
    ap = argparse.ArgumentParser(description='릴스 팀 흐름을 모니터에 재생한다')
    ap.add_argument('--url', default=os.environ.get('AGENT_MONITOR_URL', 'http://127.0.0.1:8790'))
    ap.add_argument('--speed', type=float, default=1.0)
    ap.add_argument('--keep', action='store_true', help='시작할 때 화면을 비우지 않는다')
    a = ap.parse_args(argv)
    base = a.url.rstrip('/')
    with open(os.path.join(HERE, 'hosts.json'), encoding='utf-8') as f:
        cfg = json.load(f)
    hosts = cfg['hosts']
    pause = lambda sec: time.sleep(max(0.0, sec) / max(0.1, a.speed))

    try:
        if not a.keep:
            post(base, '/api/reset', {})
        post(base, '/api/agent', {'id': 'reel-pd', 'state': 'working', 'name': cfg.get('lead', 'PD'), 'project': 'reel-team'})
    except (urllib.error.URLError, OSError) as e:
        print('모니터에 연결할 수 없습니다 (%s): %s\n먼저 agent-monitor 폴더에서 python3 start.py 를 켜세요.' % (base, e))
        return 1
    print('재생 시작: 호스트 %d명' % len(hosts))
    pause(1.5)
    for h in hosts:
        post(base, '/api/agent', {'id': 'host-%d' % h['n'], 'parent': 'reel-pd', 'state': 'working',
                                  'name': h['name'], 'role': cfg.get('role', 'host')})
        pause(0.6)

    start = time.time()
    for h in sorted(hosts, key=lambda h: h['seconds']):
        pause(h['seconds'] - (time.time() - start) * a.speed)
        name = '%d_%s.mp4' % (h['n'], h['slug'])
        local = os.path.join(HERE, 'media', name)
        body = {'id': 'host-%d' % h['n'], 'state': 'reported', 'text': '%s 릴스 완성 · %s' % (h['name'], h['topic'])}
        media = local if os.path.isfile(local) else h.get('remote')
        if media:
            body['media_url'] = media
        r = post(base, '/api/agent', body)
        shown = '결과물 표시' if r['agent'].get('media') else '영상 파일 없음: media/' + name
        print('%d번 %s 완료 (%s)' % (h['n'], h['name'], shown))

    pause(3)
    post(base, '/api/agent', {'id': 'reel-pd', 'state': 'waiting',
                              'text': '릴스 %d개 모두 완성했습니다. 확인 부탁드려요.' % len(hosts)})
    print('끝. 모니터에서 확인을 누르면 조용해집니다.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
