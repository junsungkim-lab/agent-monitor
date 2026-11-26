#!/usr/bin/env python3
"""agent-monitor 훅 설치/제거 (Python 3 표준 라이브러리만 사용)

    python3 start.py                      # 훅 연결 + 서버 켜기를 한 번에 (보통은 이것만)

    python3 install.py install            # 설치된 CLI를 찾아 전부 연결
    python3 install.py install claude     # 하나만 (claude | codex | gemini)
    python3 install.py install --dry-run  # 무엇을 바꿀지만 보여주기
    python3 install.py status
    python3 install.py uninstall
    python3 install.py print codex        # 직접 붙여넣을 설정만 출력

기존 설정은 건드리지 않고 agent-monitor 항목만 더하거나 뺀다.
파일을 고치기 전에 같은 폴더에 .bak-agent-monitor-<시각> 백업을 남긴다.
"""
import argparse
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(ROOT, 'hooks', 'agent_hook.py')
MARK = 'agent_hook.py'
DEFAULT_URL = 'http://127.0.0.1:8790'
TARGETS = ('claude', 'codex', 'gemini')
LABEL = {'claude': 'Claude Code', 'codex': 'Codex', 'gemini': 'Gemini CLI'}

CLAUDE_NOTICES = 'permission_prompt|idle_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input'


def home():
    return os.path.expanduser('~')


def config_path(target):
    if target == 'claude':
        return os.path.join(home(), '.claude', 'settings.json')
    if target == 'codex':
        base = os.environ.get('CODEX_HOME') or os.path.join(home(), '.codex')
        return os.path.join(base, 'hooks.json')
    return os.path.join(home(), '.gemini', 'settings.json')


def command(target, url, preview=True):
    cmd = '"%s" "%s" %s' % (sys.executable, HOOK, target)
    if url != DEFAULT_URL:
        cmd += ' --url %s' % url
    if not preview:
        cmd += ' --no-preview'
    return cmd


def wanted(target, url, preview=True):
    """CLI별로 등록할 훅. {이벤트 이름: 그룹}"""
    cmd = command(target, url, preview)
    if target == 'claude':
        # async: 모니터가 느리거나 꺼져 있어도 Claude 작업을 붙잡지 않는다.
        bg = {'hooks': [{'type': 'command', 'command': cmd, 'async': True}]}
        out = {ev: bg for ev in ('SessionStart', 'UserPromptSubmit', 'PermissionRequest', 'PostToolUse',
                                 'PostToolUseFailure', 'SubagentStart', 'SubagentStop', 'Stop', 'StopFailure')}
        out['Notification'] = {'matcher': CLAUDE_NOTICES,
                               'hooks': [{'type': 'command', 'command': cmd, 'async': True}]}
        out['SessionEnd'] = {'hooks': [{'type': 'command', 'command': cmd}]}
        return out
    if target == 'codex':
        bg = {'hooks': [{'type': 'command', 'command': cmd, 'async': True, 'timeout': 5}]}
        fg = {'hooks': [{'type': 'command', 'command': cmd, 'timeout': 2}]}
        out = {ev: bg for ev in ('SessionStart', 'UserPromptSubmit', 'PermissionRequest', 'PostToolUse',
                                 'SubagentStart', 'SubagentStop', 'Stop')}
        out['Interrupt'] = fg
        out['SessionEnd'] = fg
        return out
    one = {'matcher': '*', 'hooks': [{'name': 'agent-monitor', 'type': 'command', 'command': cmd, 'timeout': 2000}]}
    return {ev: one for ev in ('SessionStart', 'BeforeAgent', 'AfterAgent', 'AfterTool', 'Notification',
                               'SessionEnd')}


def is_ours(group):
    if not isinstance(group, dict):
        return False
    return any(isinstance(h, dict) and MARK in str(h.get('command') or '') for h in group.get('hooks') or [])


def strip(cfg):
    """agent-monitor 항목만 지운다. 지운 개수를 돌려준다."""
    hooks = cfg.get('hooks')
    removed = 0
    if not isinstance(hooks, dict):
        return 0
    for ev in list(hooks):
        groups = hooks[ev]
        if not isinstance(groups, list):
            continue
        kept = [g for g in groups if not is_ours(g)]
        removed += len(groups) - len(kept)
        if kept:
            hooks[ev] = kept
        else:
            del hooks[ev]
    if not hooks:
        del cfg['hooks']
    return removed


def merge(cfg, target, url, preview=True):
    strip(cfg)
    hooks = cfg.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('"hooks" 값이 객체가 아닙니다')
    for ev, group in wanted(target, url, preview).items():
        groups = hooks.setdefault(ev, [])
        if not isinstance(groups, list):
            raise ValueError('hooks.%s 값이 배열이 아닙니다' % ev)
        groups.append(json.loads(json.dumps(group)))
    return cfg


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as fh:
        text = fh.read()
    if not text.strip():
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError('최상위가 JSON 객체가 아닙니다')
    return data


def save(path, cfg):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        backup = '%s.bak-agent-monitor-%s' % (path, time.strftime('%Y%m%d-%H%M%S'))
        shutil.copy2(path, backup)
        print('   백업: %s' % backup)
    tmp = path + '.tmp-agent-monitor'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write('\n')
    os.replace(tmp, path)


def count_ours(cfg):
    hooks = cfg.get('hooks')
    if not isinstance(hooks, dict):
        return 0
    return sum(1 for groups in hooks.values() if isinstance(groups, list) for g in groups if is_ours(g))


def codex_toml_has_hooks():
    path = os.path.join(os.path.dirname(config_path('codex')), 'config.toml')
    try:
        with open(path, encoding='utf-8') as fh:
            return any(line.lstrip().startswith(('[hooks', '[[hooks')) for line in fh)
    except OSError:
        return False


def after_note(target):
    if target == 'claude':
        return '   이미 열려 있는 Claude Code 세션은 다시 시작해야 훅이 적용됩니다.'
    if target == 'codex':
        return '   Codex를 실행한 뒤 /hooks 에서 새 훅을 검토하고 신뢰해야 실행됩니다.'
    return '   Gemini CLI를 다시 시작하세요. /hooks panel 에서 등록된 훅을 확인할 수 있습니다.'


def up_to_date(cfg, target, url, preview=True):
    """우리 훅이 이미 원하는 모양 그대로 들어 있는지."""
    want = wanted(target, url, preview)
    hooks = cfg.get('hooks')
    found = {}
    if isinstance(hooks, dict):
        for ev, groups in hooks.items():
            ours = [g for g in groups if is_ours(g)] if isinstance(groups, list) else []
            if ours:
                found[ev] = ours
    return set(found) == set(want) and all(len(g) == 1 and g[0] == want[ev] for ev, g in found.items())


def do_install(target, url, dry, explicit, preview=True):
    path = config_path(target)
    if not explicit and not os.path.isdir(os.path.dirname(path)):
        print('- %s: 설정 폴더가 없어 건너뜁니다 (%s)' % (LABEL[target], os.path.dirname(path)))
        return True
    try:
        cfg = load(path)
        if up_to_date(cfg, target, url, preview):
            print('= %s: 이미 연결되어 있습니다' % LABEL[target])
            return True
        cfg = merge(cfg, target, url, preview)
    except (ValueError, OSError) as e:
        print('! %s: %s 을(를) 읽을 수 없어 건드리지 않았습니다 (%s)' % (LABEL[target], path, e))
        print('  직접 넣으려면: python3 install.py print %s' % target)
        return False
    if dry:
        print('- %s: %s 에 훅 %d개를 등록할 예정 (dry-run, 파일은 그대로)' % (LABEL[target], path, count_ours(cfg)))
        return True
    print('+ %s: %s' % (LABEL[target], path))
    save(path, cfg)
    print('   훅 %d개 등록' % count_ours(cfg))
    print(after_note(target))
    if target == 'codex' and codex_toml_has_hooks():
        print('   주의: config.toml 에도 [hooks] 가 있습니다. Codex는 한 폴더에서 두 형식을 같이 쓰면 경고합니다.')
    return True


def do_uninstall(target, dry):
    path = config_path(target)
    if not os.path.exists(path):
        return True
    try:
        cfg = load(path)
    except (ValueError, OSError) as e:
        print('! %s: %s 을(를) 읽을 수 없습니다 (%s)' % (LABEL[target], path, e))
        return False
    n = strip(cfg)
    if n == 0:
        print('- %s: 등록된 agent-monitor 훅 없음' % LABEL[target])
        return True
    if dry:
        print('- %s: 훅 %d개를 지울 예정 (dry-run)' % (LABEL[target], n))
        return True
    print('- %s: %s' % (LABEL[target], path))
    save(path, cfg)
    print('   훅 %d개 제거' % n)
    return True


def do_status(target):
    path = config_path(target)
    try:
        n = count_ours(load(path))
    except (ValueError, OSError) as e:
        print('! %s: 읽기 실패 (%s)' % (LABEL[target], e))
        return
    state = '훅 %d개 등록됨' % n if n else '미설치'
    print('%-12s %s  (%s)' % (LABEL[target], state, path))


def ensure(url=DEFAULT_URL, preview=True):
    """설치된 CLI마다 훅이 연결돼 있는지 보고, 빠졌거나 달라졌을 때만 고친다. (start.py 용)"""
    found = False
    for target in TARGETS:
        if os.path.isdir(os.path.dirname(config_path(target))):
            found = True
            do_install(target, url.rstrip('/'), dry=False, explicit=False, preview=preview)
    if not found:
        print('연결할 CLI를 찾지 못했습니다 (Claude Code, Codex, Gemini CLI).')
        print('CLI를 설치한 뒤 다시 실행하면 자동으로 연결됩니다.')
    return found


def main():
    ap = argparse.ArgumentParser(description='agent-monitor 훅 설치/제거')
    ap.add_argument('action', choices=('install', 'uninstall', 'status', 'print'))
    ap.add_argument('targets', nargs='*', help='claude codex gemini (생략하면 전부)')
    ap.add_argument('--url', default=DEFAULT_URL, help='모니터 서버 주소 (기본 %s)' % DEFAULT_URL)
    ap.add_argument('--dry-run', action='store_true', help='파일을 고치지 않고 계획만 출력')
    ap.add_argument('--no-preview', action='store_true',
                    help='응답 미리보기·명령·결과물 주소를 보내지 않는 훅으로 등록')
    a = ap.parse_args()

    explicit = bool(a.targets) and a.targets != ['all']
    targets = a.targets if explicit else list(TARGETS)
    for t in targets:
        if t not in TARGETS:
            ap.error('알 수 없는 대상: %s (claude, codex, gemini 중에서)' % t)
    url = a.url.rstrip('/')

    ok = True
    for t in targets:
        if a.action == 'install':
            ok = do_install(t, url, a.dry_run, explicit, not a.no_preview) and ok
        elif a.action == 'uninstall':
            ok = do_uninstall(t, a.dry_run) and ok
        elif a.action == 'status':
            do_status(t)
        else:
            print('# %s → %s' % (LABEL[t], config_path(t)))
            print(json.dumps({'hooks': {ev: [g] for ev, g in wanted(t, url, not a.no_preview).items()}},
                             ensure_ascii=False, indent=2))
    if a.action == 'install' and ok and not a.dry_run:
        print('\n모니터 서버 켜기: python3 "%s"' % os.path.join(ROOT, 'server.py'))
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
