#!/usr/bin/env python3
"""agent-monitor 한 번에 시작하기: 훅 연결 + 모니터 서버 켜기

    python3 start.py              # 설치된 CLI에 훅을 연결하고 서버를 켠다
    python3 start.py --open       # 켜지면 브라우저로 화면까지 연다
    python3 start.py --lan        # 아이패드 등 다른 기기에서도 보기 (토큰 필요)
    python3 start.py --no-hooks   # 설정은 건드리지 않고 서버만
    python3 start.py --no-preview # 응답 미리보기·명령·결과물을 보내지도 보여주지도 않기
    python3 start.py --install-voice  # AI 음성(오픈소스 Supertonic 3)을 설치하고 켜기

훅은 처음 한 번만 등록된다. 이미 연결돼 있으면 설정 파일을 건드리지 않는다.
훅을 지우려면: python3 install.py uninstall
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import install  # noqa: E402
import server   # noqa: E402


def install_voice():
    """이 파이썬에 supertonic 을 설치한다. 가상환경이 아니면 사용자 폴더(--user)에."""
    cmd = [sys.executable, '-m', 'pip', 'install', 'supertonic']
    if sys.prefix == getattr(sys, 'base_prefix', sys.prefix):
        cmd.insert(4, '--user')
    print('AI 음성 설치:', ' '.join(cmd))
    if subprocess.call(cmd) != 0:
        print('! 설치하지 못했습니다. 가상환경을 만들어 그 안에서 실행해 보세요:')
        print('  python3 -m venv .venv && .venv/bin/pip install supertonic && .venv/bin/python start.py')
        return False
    import importlib
    importlib.invalidate_caches()
    try:                                    # 방금 --user 로 깐 경로를 이 실행에도 보이게
        import site
        user_site = site.getusersitepackages()
        if user_site not in sys.path:
            sys.path.append(user_site)
    except Exception:
        pass
    print('설치 완료. 모델(약 400MB)은 서버가 처음 켜질 때 한 번 내려받습니다.\n')
    return True


def main(argv=None):
    ap = server.build_parser('훅 연결과 모니터 서버 실행을 한 번에 한다')
    ap.add_argument('--no-hooks', action='store_true', help='CLI 설정을 건드리지 않고 서버만 켠다')
    ap.add_argument('--install-voice', action='store_true', help='AI 음성(Supertonic 3)을 설치하고 켠다')
    a = ap.parse_args(argv)
    if a.install_voice and not server.Voice.installed():
        install_voice()
    if not a.no_hooks:
        try:
            install.ensure('http://127.0.0.1:%d' % a.port, preview=not a.no_preview)
        except Exception as e:                     # 훅 문제로 서버까지 못 켜는 일은 없게
            print('! 훅 연결 중 문제가 생겼습니다: %s' % e)
            print('  서버는 그대로 켭니다. 나중에 python3 install.py install 로 다시 시도하세요.')
        print()
    server.run(a)


if __name__ == '__main__':
    main()
