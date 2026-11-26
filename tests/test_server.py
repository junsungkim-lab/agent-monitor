"""서버 상태 전이와 접근 제어 테스트.  실행: python3 -m unittest discover -s tests -v"""
import http.client
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        server.STORE = server.Store(sub_ttl=0.2)
        server.CFG.update(token='', quiet=True)
        self.srv = server.make_server('127.0.0.1', 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        h = {'Content-Type': 'application/json'} if body is not None else {}
        h.update(headers or {})
        conn.request(method, path, json.dumps(body) if body is not None else None, h)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, raw

    def hook(self, source, event, sid='s1', **extra):
        p = {'hook_event_name': event, 'session_id': sid}
        p.update(extra)
        status, _ = self.req('POST', '/hook/' + source, p)
        self.assertEqual(status, 200)

    def agents(self):
        return {a['id']: a for a in self.req('GET', '/api/state')[1]['agents']}


class ClaudeFlow(Base):
    def test_session_lifecycle(self):
        self.hook('claude', 'SessionStart', project='shop')
        a = self.agents()['claude:s1']
        self.assertEqual((a['state'], a['kind'], a['project']), ('idle', 'session', 'shop'))

        self.hook('claude', 'UserPromptSubmit')
        self.assertEqual(self.agents()['claude:s1']['state'], 'working')

        self.hook('claude', 'Stop', background_count=0)
        a = self.agents()['claude:s1']
        self.assertEqual((a['state'], a['acked']), ('waiting', False))

        self.assertEqual(self.req('POST', '/api/ack', {'id': 'claude:s1'})[1]['acked'], 1)
        self.assertTrue(self.agents()['claude:s1']['acked'])

        self.hook('claude', 'UserPromptSubmit')          # 다음 프롬프트 = 봤다는 뜻
        a = self.agents()['claude:s1']
        self.assertEqual((a['state'], a['acked']), ('working', False))

        self.hook('claude', 'SessionEnd', reason='other')
        self.assertEqual(self.agents(), {})

    def test_stop_with_background_work_is_not_done(self):
        self.hook('claude', 'UserPromptSubmit')
        self.hook('claude', 'Stop', background_count=2)
        a = self.agents()['claude:s1']
        self.assertEqual((a['state'], a['note']), ('working', 'background'))

    def test_permission_blocks_then_tool_resumes(self):
        self.hook('claude', 'UserPromptSubmit')
        self.hook('claude', 'PermissionRequest', tool_name='Bash')
        a = self.agents()['claude:s1']
        self.assertEqual((a['state'], a['note']), ('blocked', 'Bash'))
        self.hook('claude', 'PostToolUse', tool_name='Bash')
        self.assertEqual(self.agents()['claude:s1']['state'], 'working')

    def test_late_tool_event_does_not_undo_stop(self):
        self.hook('claude', 'UserPromptSubmit')
        self.hook('claude', 'Stop')
        self.hook('claude', 'PostToolUse', tool_name='Read')   # 비동기 훅이 늦게 도착
        self.assertEqual(self.agents()['claude:s1']['state'], 'waiting')

    def test_notifications(self):
        self.hook('claude', 'UserPromptSubmit')
        self.hook('claude', 'Notification', notification_type='permission_prompt')
        self.assertEqual(self.agents()['claude:s1']['state'], 'blocked')
        self.hook('claude', 'PostToolUse')
        self.hook('claude', 'Notification', notification_type='idle_prompt')   # 사용자가 중단한 뒤
        self.assertEqual(self.agents()['claude:s1']['state'], 'idle')
        self.hook('claude', 'UserPromptSubmit')
        self.hook('claude', 'Stop')
        self.hook('claude', 'Notification', notification_type='idle_prompt')
        self.assertEqual(self.agents()['claude:s1']['state'], 'waiting')

    def test_stop_failure(self):
        self.hook('claude', 'UserPromptSubmit')
        self.hook('claude', 'StopFailure', error='rate_limit')
        a = self.agents()['claude:s1']
        self.assertEqual((a['state'], a['note']), ('error', 'rate_limit'))

    def test_subagents(self):
        self.hook('claude', 'UserPromptSubmit', project='shop')
        self.hook('claude', 'SubagentStart', agent_id='a1', agent_type='Explore')
        self.hook('claude', 'SubagentStart', agent_id='a2', agent_type='test-runner')
        ag = self.agents()
        sub = ag['claude:s1:a1']
        self.assertEqual((sub['kind'], sub['parent'], sub['role'], sub['state'], sub['project']),
                         ('subagent', 'claude:s1', 'Explore', 'working', 'shop'))
        self.assertEqual(ag['claude:s1']['state'], 'working')
        self.assertEqual(len({a['seat'] for a in ag.values()}), 3)

        # 서브에이전트 안에서 난 승인 요청은 그 서브에이전트 타일로 간다
        self.hook('claude', 'PermissionRequest', agent_id='a2', tool_name='Bash')
        ag = self.agents()
        self.assertEqual(ag['claude:s1:a2']['state'], 'blocked')
        self.assertEqual(ag['claude:s1']['state'], 'working')

        self.hook('claude', 'SubagentStop', agent_id='a1', agent_type='Explore')
        self.assertEqual(self.agents()['claude:s1:a1']['state'], 'reported')
        time.sleep(0.3)
        server.STORE.sweep()                               # 보고 뒤 잠시 있다가 퇴장
        self.assertNotIn('claude:s1:a1', self.agents())
        self.assertIn('claude:s1:a2', self.agents())

        self.hook('claude', 'SubagentStop', agent_id='zzz')   # 모르는 서브에이전트
        self.assertNotIn('claude:s1:zzz', self.agents())

        self.hook('claude', 'SessionEnd')                  # 세션이 끝나면 딸린 타일도 같이
        self.assertEqual(self.agents(), {})

    def test_event_for_unknown_session_creates_it(self):
        self.hook('claude', 'Stop', sid='late')            # 서버를 나중에 켠 경우
        self.assertEqual(self.agents()['claude:late']['state'], 'waiting')


class OtherClis(Base):
    def test_codex(self):
        self.hook('codex', 'SessionStart', project='api')
        self.hook('codex', 'UserPromptSubmit')
        self.hook('codex', 'SubagentStart', agent_id='w1', agent_type='reviewer')
        self.hook('codex', 'PermissionRequest', tool_name='apply_patch')
        self.assertEqual(self.agents()['codex:s1']['state'], 'blocked')
        self.hook('codex', 'PostToolUse')
        self.hook('codex', 'SubagentStop', agent_id='w1')
        self.hook('codex', 'Stop')
        ag = self.agents()
        self.assertEqual(ag['codex:s1']['state'], 'waiting')
        self.assertEqual(ag['codex:s1:w1']['state'], 'reported')
        self.hook('codex', 'UserPromptSubmit')
        self.hook('codex', 'Interrupt')
        self.assertEqual(self.agents()['codex:s1']['state'], 'idle')

    def test_gemini(self):
        self.hook('gemini', 'SessionStart', project='docs')
        self.hook('gemini', 'BeforeAgent')
        self.assertEqual(self.agents()['gemini:s1']['state'], 'working')
        self.hook('gemini', 'Notification', notification_type='ToolPermission')
        self.assertEqual(self.agents()['gemini:s1']['state'], 'blocked')
        self.hook('gemini', 'AfterTool')
        self.assertEqual(self.agents()['gemini:s1']['state'], 'working')
        self.hook('gemini', 'AfterAgent')
        self.assertEqual(self.agents()['gemini:s1']['state'], 'waiting')
        self.hook('gemini', 'SessionEnd', reason='exit')
        self.assertEqual(self.agents(), {})

    def test_same_session_id_on_two_clis_stays_separate(self):
        self.hook('claude', 'UserPromptSubmit')
        self.hook('codex', 'UserPromptSubmit')
        self.assertEqual(set(self.agents()), {'claude:s1', 'codex:s1'})


class Api(Base):
    def test_ack_all_and_mode(self):
        for sid in ('a', 'b'):
            self.hook('claude', 'Stop', sid=sid)
        self.hook('claude', 'UserPromptSubmit', sid='c')
        self.assertEqual(self.req('POST', '/api/ack', {'all': True})[1]['acked'], 2)
        self.assertEqual(self.req('POST', '/api/ack', {'all': True})[1]['acked'], 0)
        self.assertEqual(self.req('POST', '/api/mode', {'mode': 'chaos'})[1]['mode'], 'chaos')
        self.assertEqual(self.req('GET', '/api/state')[1]['mode'], 'chaos')
        self.assertEqual(self.req('POST', '/api/mode', {'mode': 'loud'})[0], 400)

    def test_custom_agent(self):
        status, body = self.req('POST', '/api/agent', {'id': 'nightly', 'state': 'waiting', 'project': 'ci'})
        self.assertEqual(status, 200)
        self.assertEqual(body['agent']['id'], 'custom:nightly')
        self.assertEqual(self.req('POST', '/api/agent', {'id': 'x', 'state': 'sleeping'})[0], 400)

    def test_bad_requests(self):
        self.assertEqual(self.req('POST', '/hook/claude', {'session_id': 's'})[0], 400)
        self.assertEqual(self.req('POST', '/hook/cursor', {'hook_event_name': 'Stop', 'session_id': 's'})[0], 400)
        self.assertEqual(self.req('POST', '/hook/claude', {'a': 1}, {'Content-Type': 'text/plain'})[0], 400)
        self.assertEqual(self.req('GET', '/nope')[0], 404)
        self.assertEqual(self.req('GET', '/../server.py')[0], 404)
        self.assertEqual(self.req('GET', '/%2e%2e/server.py')[0], 404)

    def test_long_text_is_clipped(self):
        self.hook('claude', 'UserPromptSubmit', project='p' * 500, session_title='t' * 500)
        a = self.agents()['claude:s1']
        self.assertLessEqual(len(a['project']), 60)
        self.assertLessEqual(len(a['title']), 60)

    def test_index_served(self):
        status, body = self.req('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'<!doctype html>', body[:200].lower())

    def test_sse_snapshot_then_live_event(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        conn.request('GET', '/events')
        r = conn.getresponse()
        self.assertEqual(r.status, 200)

        def next_event():
            while True:
                line = r.fp.readline().decode()
                if line.startswith('data: '):
                    return json.loads(line[6:])

        self.assertEqual(next_event()['type'], 'snapshot')
        self.hook('claude', 'Stop')
        evt = next_event()
        self.assertEqual((evt['type'], evt['agent']['id']), ('agent', 'claude:s1'))
        conn.close()


class Access(Base):
    def test_rebinding_host_rejected(self):
        self.assertEqual(self.req('GET', '/api/state', headers={'Host': 'evil.example.com'})[0], 403)
        self.assertEqual(self.req('POST', '/api/reset', {}, {'Host': 'evil.example.com'})[0], 403)
        self.assertEqual(self.req('GET', '/api/state', headers={'Host': 'localhost:%d' % self.port})[0], 200)

    def test_host_allowed(self):
        ok = ['localhost', 'localhost:8790', '127.0.0.1:8790', '192.168.0.5:8790', '[::1]:8790', 'mac.local:8790']
        bad = ['', 'evil.com', 'evil.com:8790', '127.0.0.1.evil.com', 'localhost.evil.com']
        for h in ok:
            self.assertTrue(server.host_allowed(h), h)
        for h in bad:
            self.assertFalse(server.host_allowed(h), h)

    def test_loopback_detection(self):
        self.assertTrue(server.is_loopback('127.0.0.1'))
        self.assertTrue(server.is_loopback('::1'))
        self.assertTrue(server.is_loopback('::ffff:127.0.0.1'))
        self.assertFalse(server.is_loopback('192.168.0.5'))

    def test_remote_client_needs_token(self):
        """다른 기기에서 온 요청은 토큰이 맞아야 한다 (루프백 판정을 꺼서 흉내 낸다)."""
        original = server.is_loopback
        server.is_loopback = lambda ip: False
        server.CFG['token'] = 'secret-token'
        try:
            self.assertEqual(self.req('GET', '/api/state')[0], 401)
            self.assertEqual(self.req('GET', '/api/state?token=wrong')[0], 401)
            self.assertEqual(self.req('GET', '/api/state?token=secret-token')[0], 200)
            self.assertEqual(self.req('GET', '/api/state', headers={'Authorization': 'Bearer secret-token'})[0], 200)
            self.assertEqual(self.req('GET', '/api/state', headers={'Cookie': 'am_token=secret-token'})[0], 200)
            self.assertEqual(self.req('POST', '/hook/claude', {'hook_event_name': 'Stop', 'session_id': 'x'})[0], 401)
            server.CFG['token'] = ''                      # --lan 없이 켠 서버는 원격을 전부 거절
            self.assertEqual(self.req('GET', '/api/state?token=')[0], 401)
        finally:
            server.is_loopback = original


class HookScript(Base):
    def run_hook(self, source, payload, *args):
        env = dict(os.environ, AGENT_MONITOR_URL='http://127.0.0.1:%d' % self.port)
        return subprocess.run([sys.executable, os.path.join(ROOT, 'hooks', 'agent_hook.py'), source, *args],
                              input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=10)

    PAYLOAD = {
        'hook_event_name': 'Stop', 'session_id': 'abc', 'cwd': '/Users/me/hidden-client/shop',
        'transcript_path': '/Users/me/.claude/x.jsonl', 'prompt': 'PRIVATE PROMPT', 'background_tasks': [],
        'last_assistant_message': '## 결과\n\n결제 재시도 로직을 고쳤습니다.\n' + '중간 설명 줄입니다. ' * 40
                                  + '\n\n테스트 42개가 통과했습니다.\n배포도 지금 진행할까요?'}

    def test_preview_sends_a_short_summary_but_not_the_rest(self):
        r = self.run_hook('claude', self.PAYLOAD)
        self.assertEqual((r.returncode, r.stdout), (0, ''))
        a = self.agents()['claude:abc']
        self.assertEqual((a['state'], a['project'], a['ask']), ('waiting', 'shop', True))
        self.assertTrue(a['text'].endswith('배포도 지금 진행할까요?'))
        self.assertLessEqual(len(a['text']), 240)
        dump = json.dumps(a, ensure_ascii=False)
        for leaked in ('PRIVATE PROMPT', 'hidden-client', 'transcript', '/Users', '##'):
            self.assertNotIn(leaked, dump)

    def test_no_preview_sends_no_text_at_all(self):
        self.run_hook('claude', self.PAYLOAD, '--no-preview')
        a = self.agents()['claude:abc']
        self.assertEqual((a['state'], a['text'], a['ask'], a['media'], a['detail']), ('waiting', '', False, [], ''))

    def test_statement_ending_shows_the_opening_lines(self):
        self.run_hook('codex', {'hook_event_name': 'Stop', 'session_id': 'c9',
                                'last_assistant_message': '마이그레이션을 끝냈습니다.\n\n- users 테이블 변경\n- 인덱스 추가'})
        a = self.agents()['codex:c9']
        self.assertEqual(a['ask'], False)
        self.assertTrue(a['text'].startswith('마이그레이션을 끝냈습니다.'))

    def test_pending_command_is_shown_with_secrets_masked(self):
        self.run_hook('claude', {'hook_event_name': 'UserPromptSubmit', 'session_id': 'abc'})
        self.run_hook('claude', {
            'hook_event_name': 'PermissionRequest', 'session_id': 'abc', 'tool_name': 'Bash',
            'tool_input': {'command': 'curl -H "Authorization: Bearer abc.def.ghi" -d token=hunter2 '
                                      'https://api.example.com/deploy && export API_KEY=sk-live-0123456789abcdefghijklmnopqrstuv'}})
        a = self.agents()['claude:abc']
        self.assertEqual((a['state'], a['note']), ('blocked', 'Bash'))
        self.assertIn('https://api.example.com/deploy', a['detail'])
        for secret in ('abc.def.ghi', 'hunter2', 'sk-live-0123456789'):
            self.assertNotIn(secret, a['detail'])

    def test_masking_leaves_ordinary_text_and_paths_alone(self):
        sys.path.insert(0, os.path.join(ROOT, 'hooks'))
        import agent_hook
        plain = 'the token refresh flow and password reset page now share one helper'
        self.assertEqual(agent_hook.mask(plain), plain)
        path = 'src/components/checkout/payment/retry/index.tsx'
        self.assertEqual(agent_hook.mask('edit ' + path), 'edit ' + path)
        self.assertEqual(agent_hook.mask('GITHUB_TOKEN=ghp_abc123'), 'GITHUB_TOKEN=***')
        self.assertEqual(agent_hook.mask('{"api_key": "xyz"}'), '{"api_key": "***"}')
        self.assertEqual(agent_hook.mask('login --password hunter2 now'), 'login --password *** now')

    def test_media_urls_are_picked_out_of_tool_results(self):
        self.run_hook('claude', {'hook_event_name': 'SubagentStart', 'session_id': 'abc', 'agent_id': 'h1',
                                 'agent_type': 'host'})
        video = 'https://cdn.example.com/out/host1.mp4?X-Amz-Signature=abcdef&X-Amz-Expires=3600'
        self.run_hook('claude', {
            'hook_event_name': 'PostToolUse', 'session_id': 'abc', 'agent_id': 'h1',
            'tool_name': 'mcp__Higgsfield__jobs_wait',
            'tool_response': {'jobs': [{'id': 'j1', 'status': 'completed', 'result': {'url': video}},
                                       {'docs': 'https://example.com/help.html'}],
                              'thumb': 'https://cdn.example.com/out/host1.jpg'}})
        ag = self.agents()
        self.assertEqual(ag['claude:abc:h1']['media'], [video, 'https://cdn.example.com/out/host1.jpg'])
        self.assertEqual(ag['claude:abc']['media'], [])

    def test_background_tasks_counted(self):
        self.run_hook('claude', {'hook_event_name': 'Stop', 'session_id': 'abc',
                                 'background_tasks': [{'id': 't1', 'type': 'shell'}]})
        self.assertEqual(self.agents()['claude:abc']['state'], 'working')

    def test_url_argument(self):
        env = {k: v for k, v in os.environ.items() if k != 'AGENT_MONITOR_URL'}
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'hooks', 'agent_hook.py'), 'gemini',
                            '--url', 'http://127.0.0.1:%d' % self.port],
                           input=json.dumps({'hook_event_name': 'BeforeAgent', 'session_id': 'g1'}),
                           text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual((r.returncode, r.stdout), (0, ''))
        self.assertEqual(self.agents()['gemini:g1']['state'], 'working')

    def test_never_fails_or_prints(self):
        for payload_text in ('not json', '[1,2]', ''):
            r = subprocess.run([sys.executable, os.path.join(ROOT, 'hooks', 'agent_hook.py'), 'codex'],
                               input=payload_text, text=True, capture_output=True, timeout=10)
            self.assertEqual((r.returncode, r.stdout), (0, ''))
        env = dict(os.environ, AGENT_MONITOR_URL='http://127.0.0.1:9')      # 서버가 꺼진 경우
        started = time.time()
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'hooks', 'agent_hook.py'), 'claude'],
                           input=json.dumps({'hook_event_name': 'Stop', 'session_id': 'x'}),
                           text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual((r.returncode, r.stdout), (0, ''))
        self.assertLess(time.time() - started, 3)



class Preview(Base):
    """끝났을 때 보여줄 결과, 기다리는 명령, 결과물 주소."""

    def test_result_text_and_question(self):
        self.hook('claude', 'UserPromptSubmit')
        self.hook('claude', 'Stop', head='로그인 버그를 고쳤습니다.', tail='테스트도 통과했습니다.')
        a = self.agents()['claude:s1']
        self.assertEqual((a['text'], a['ask']), ('로그인 버그를 고쳤습니다.', False))

        self.hook('claude', 'UserPromptSubmit')                       # 새 턴이면 지난 결과는 지운다
        self.assertEqual(self.agents()['claude:s1']['text'], '')

        self.hook('claude', 'Stop', head='두 가지 방법이 있습니다.', tail='A안과 B안 중 어느 쪽으로 할까요?')
        a = self.agents()['claude:s1']
        self.assertEqual((a['text'], a['ask']), ('A안과 B안 중 어느 쪽으로 할까요?', True))

    def test_pending_command_cleared_after_approval(self):
        self.hook('claude', 'UserPromptSubmit')
        self.hook('claude', 'PermissionRequest', tool_name='Bash', detail='npm run deploy')
        self.assertEqual(self.agents()['claude:s1']['detail'], 'npm run deploy')
        self.hook('claude', 'Notification', notification_type='permission_prompt', detail='Claude needs permission')
        self.assertEqual(self.agents()['claude:s1']['detail'], 'npm run deploy')   # 더 구체적인 쪽을 유지
        self.hook('claude', 'PostToolUse', tool_name='Bash')
        a = self.agents()['claude:s1']
        self.assertEqual((a['state'], a['detail']), ('working', ''))

    def test_media_is_validated_deduped_and_capped(self):
        self.hook('claude', 'UserPromptSubmit')
        good = ['https://cdn.example.com/%d.mp4' % i for i in range(6)]
        self.hook('claude', 'PostToolUse', media=['javascript:alert(1)', 'data:image/png;base64,AAAA', 'file:///etc/passwd',
                                                   'https://ok.example.com/a b.mp4', good[0], good[0], 7, None])
        self.assertEqual(self.agents()['claude:s1']['media'], [good[0]])
        self.hook('claude', 'PostToolUse', media=good[1:])
        media = self.agents()['claude:s1']['media']
        self.assertEqual(len(media), 4)
        self.assertEqual(media[0], good[1])                                  # 최신 결과가 맨 앞

    def test_subagent_with_result_stays_until_next_turn(self):
        self.hook('claude', 'UserPromptSubmit')
        self.hook('claude', 'SubagentStart', agent_id='a1', agent_type='host')
        self.hook('claude', 'SubagentStart', agent_id='a2', agent_type='host')
        self.hook('claude', 'PostToolUse', agent_id='a1', media=['https://cdn.example.com/1.mp4'])
        self.hook('claude', 'SubagentStop', agent_id='a1', head='영상 완성했습니다.', tail='영상 완성했습니다.')
        self.hook('claude', 'SubagentStop', agent_id='a2', tail='이대로 진행할까요?')
        ag = self.agents()
        self.assertEqual((ag['claude:s1:a1']['text'], ag['claude:s1:a1']['ask']), ('영상 완성했습니다.', False))
        self.assertEqual(ag['claude:s1:a2']['ask'], False)                 # 서브에이전트의 질문은 부모가 받는다
        time.sleep(0.3)
        server.STORE.sweep()
        ag = self.agents()
        self.assertIn('claude:s1:a1', ag)                                  # 결과물이 있으면 남는다
        self.assertNotIn('claude:s1:a2', ag)
        self.hook('claude', 'UserPromptSubmit')
        self.assertIn('claude:s1:a1', self.agents())                       # 방금 보고한 결과는 새 턴에도 잠깐 남는다
        server.STORE.result_keep = 0
        self.hook('claude', 'UserPromptSubmit')
        self.assertNotIn('claude:s1:a1', self.agents())

    def test_preview_off_stores_nothing(self):
        server.STORE.preview = False
        self.hook('claude', 'UserPromptSubmit')
        self.hook('claude', 'PostToolUse', media=['https://cdn.example.com/1.mp4'])
        self.hook('claude', 'PermissionRequest', tool_name='Bash', detail='rm -rf build')
        self.hook('claude', 'Stop', head='끝', tail='진행할까요?')
        a = self.agents()['claude:s1']
        self.assertEqual((a['text'], a['ask'], a['detail'], a['media']), ('', False, '', []))
        self.assertEqual(a['state'], 'waiting')

    def test_custom_agent_with_result(self):
        self.req('POST', '/api/agent', {'id': 'lead', 'state': 'working', 'name': '총괄'})
        status, body = self.req('POST', '/api/agent', {
            'id': 'host1', 'state': 'waiting', 'name': '재테크 민지', 'role': 'host', 'parent': 'lead',
            'text': '영상 완성', 'media_url': 'https://cdn.example.com/host1.mp4'})
        a = body['agent']
        self.assertEqual(status, 200)
        self.assertEqual((a['name'], a['kind'], a['parent'], a['text'], a['media']),
                         ('재테크 민지', 'subagent', 'custom:lead', '영상 완성', ['https://cdn.example.com/host1.mp4']))
        a = self.req('POST', '/api/agent', {'id': 'host1', 'state': 'waiting', 'media_url': 'ftp://x/y.mp4'})[1]['agent']
        self.assertEqual((a['media'], a['media_ignored']), (['https://cdn.example.com/host1.mp4'], 1))   # 잘못된 값은 무시
        a = self.req('POST', '/api/agent', {'id': 'host1', 'state': 'waiting', 'media_url': ''})[1]['agent']
        self.assertEqual(a['media'], [])                                     # 빈 값은 결과물을 내린다



class Mcp(Base):
    """mcp_server.py 를 실제 stdio 프로토콜로 호출한다."""

    def talk(self, messages, url=None):
        env = dict(os.environ, AGENT_MONITOR_URL=url or 'http://127.0.0.1:%d' % self.port)
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'mcp_server.py')],
                           input=''.join(json.dumps(m) + '\n' for m in messages),
                           text=True, capture_output=True, env=env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]

    def call(self, i, name, args):
        return {'jsonrpc': '2.0', 'id': i, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}}

    def test_handshake_and_tool_list(self):
        out = self.talk([
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'nope'}])
        self.assertEqual([m['id'] for m in out], [1, 2, 3])                 # 알림에는 답하지 않는다
        self.assertEqual(out[0]['result']['serverInfo']['name'], 'agent-monitor')
        self.assertEqual({t['name'] for t in out[1]['result']['tools']},
                         {'monitor_report', 'monitor_remove', 'monitor_state', 'monitor_reset'})
        self.assertEqual(out[2]['error']['code'], -32601)

    def test_report_result_then_remove(self):
        video = 'https://cdn.example.com/host1.mp4'
        out = self.talk([
            self.call(1, 'monitor_report', {'id': 'lead', 'state': 'working', 'name': '총괄'}),
            self.call(2, 'monitor_report', {'id': 'host-1', 'state': 'reported', 'name': '재테크 민지', 'parent': 'lead',
                                            'role': 'host', 'text': '영상 완성', 'media_url': video}),
            self.call(3, 'monitor_state', {})])
        self.assertFalse(out[0]['result']['isError'])
        self.assertIn('result shown', out[1]['result']['content'][0]['text'])
        listed = json.loads(out[2]['result']['content'][0]['text'])
        self.assertEqual(len(listed), 2)
        a = self.agents()['custom:host-1']
        self.assertEqual((a['name'], a['parent'], a['state'], a['media']), ('재테크 민지', 'custom:lead', 'reported', [video]))

        out = self.talk([self.call(1, 'monitor_remove', {'id': 'lead'}), self.call(2, 'monitor_report', {'id': 'x', 'state': 'napping'})])
        self.assertEqual(self.agents(), {})                                 # 딸린 타일도 같이 지워진다
        self.assertTrue(out[1]['result']['isError'])                        # 잘못된 상태는 오류로 돌려준다

    def test_monitor_down_is_reported_not_fatal(self):
        out = self.talk([self.call(1, 'monitor_report', {'id': 'a', 'state': 'working'})], url='http://127.0.0.1:9')
        self.assertTrue(out[0]['result']['isError'])
        self.assertIn('not reachable', out[0]['result']['content'][0]['text'])



class LocalMedia(Base):
    """이 컴퓨터에 있는 결과물 파일: 훅·API·MCP 가 경로를 넘기면 /media/ 주소로 띄운다."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix='am-media-')
        os.makedirs(os.path.join(self.tmp, 'out'))
        self.video = os.path.join(self.tmp, 'out', '1_minji.mp4')
        self.data = bytes(range(256)) * 40                                   # 10240 바이트
        with open(self.video, 'wb') as f:
            f.write(self.data)

    def tearDown(self):
        super().tearDown()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_hook(self, payload):
        env = dict(os.environ, AGENT_MONITOR_URL='http://127.0.0.1:%d' % self.port)
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'hooks', 'agent_hook.py'), 'claude'],
                           input=json.dumps(payload), text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual((r.returncode, r.stdout), (0, ''))

    def raw(self, method, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        conn.request(method, path, headers=headers or {})
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, dict(r.getheaders()), body

    def test_subagent_reports_a_local_file_and_the_tile_plays_it(self):
        base = {'session_id': 'lead', 'cwd': self.tmp}
        self.run_hook(dict(base, hook_event_name='UserPromptSubmit'))
        self.run_hook(dict(base, hook_event_name='SubagentStart', agent_id='a1', agent_type='reel-host'))
        self.run_hook(dict(base, hook_event_name='PostToolUse', agent_id='a1', agent_type='reel-host', tool_name='Bash',
                           tool_response={'stdout': '[agent-monitor] name: 재테크 민지\n렌더링 100%\n완성: ' + self.video,
                                          'stderr': '', 'interrupted': False}))
        a = self.agents()['claude:lead:a1']
        self.assertEqual(a['name'], '재테크 민지')
        self.assertEqual(len(a['media']), 1)
        url = a['media'][0]
        self.assertRegex(url, r'^/media/[0-9a-f]{20}\.mp4$')
        self.assertNotIn(self.tmp, json.dumps(self.req('GET', '/api/state')[1]))   # 경로는 화면에 나가지 않는다

        self.run_hook(dict(base, hook_event_name='SubagentStop', agent_id='a1', agent_type='reel-host',
                           last_assistant_message='완성했습니다. 결과물: `out/1_minji.mp4`'))
        a = self.agents()['claude:lead:a1']
        self.assertEqual((a['state'], a['media']), ('reported', [url]))     # 상대 경로도 같은 파일로

        status, h, body = self.raw('GET', url)
        self.assertEqual((status, h['Content-Type'], h['Accept-Ranges'], body), (200, 'video/mp4', 'bytes', self.data))
        status, h, body = self.raw('GET', url, {'Range': 'bytes=100-199'})
        self.assertEqual((status, h['Content-Range'], body), (206, 'bytes 100-199/10240', self.data[100:200]))
        status, h, body = self.raw('GET', url, {'Range': 'bytes=-40'})
        self.assertEqual((status, body), (206, self.data[-40:]))
        status, h, body = self.raw('GET', url, {'Range': 'bytes=0-1'})          # 사파리가 처음 보내는 요청
        self.assertEqual((status, h['Content-Range'], body), (206, 'bytes 0-1/10240', self.data[:2]))
        self.assertEqual(self.raw('GET', url, {'Range': 'bytes=99999-'})[0], 416)
        status, h, body = self.raw('HEAD', url)
        self.assertEqual((status, h['Content-Length'], body), (200, '10240', b''))

    def test_only_real_media_files_are_accepted(self):
        notes = os.path.join(self.tmp, 'out', 'notes.txt')
        with open(notes, 'w') as f:
            f.write('secret')
        _, body = self.req('POST', '/api/agent', {'id': 'w', 'state': 'waiting', 'media': [
            notes, '/etc/passwd', os.path.join(self.tmp, 'out', 'missing.mp4'), 'relative/x.mp4', self.video]})
        a = body['agent']
        self.assertEqual((len(a['media']), a['media_ignored']), (1, 4))
        self.assertEqual(self.raw('GET', '/media/0123456789abcdef0123.mp4')[0], 404)
        self.assertEqual(self.raw('GET', a['media'][0].replace('.mp4', '.png'))[0], 404)   # 확장자도 맞아야 한다
        self.assertEqual(self.raw('GET', '/media/../server.py')[0], 404)

    def test_requests_from_other_devices_cannot_register_local_files(self):
        a = server.STORE.set_custom({'id': 'w', 'state': 'waiting', 'media_url': self.video}, local=False)
        self.assertEqual((a['media'], a['media_ignored']), ([], 1))
        server.STORE.apply_hook('claude', {'hook_event_name': 'PostToolUse', 'session_id': 's', 'media': [self.video]})
        self.assertEqual(self.agents()['claude:s']['media'], [])
        known = server.STORE.set_custom({'id': 'v', 'state': 'waiting', 'media_url': self.video}, local=True)['media'][0]
        a = server.STORE.set_custom({'id': 'w', 'state': 'waiting', 'media_url': known}, local=False)
        self.assertEqual(a['media'], [known])                                # 이미 띄운 주소를 다시 쓰는 건 된다

    def test_mcp_accepts_a_local_path(self):
        out = Mcp.talk(self, [
            Mcp.call(self, 1, 'monitor_report', {'id': 'host-1', 'state': 'reported', 'media_url': self.video}),
            Mcp.call(self, 2, 'monitor_report', {'id': 'host-2', 'state': 'reported', 'media_url': '/nope/x.mp4'})])
        self.assertIn('result shown', out[0]['result']['content'][0]['text'])
        self.assertIn('not shown', out[1]['result']['content'][0]['text'])
        self.assertRegex(self.agents()['custom:host-1']['media'][0], r'^/media/')

    def test_finding_paths_is_fast_and_ignores_urls_and_bare_names(self):
        sys.path.insert(0, os.path.join(ROOT, 'hooks'))
        import agent_hook
        blob = ('A' * 5000 + '/') * 80 + ' ' + ('ab/' * 30000) + 'x.mp4'
        started = time.time()
        self.assertEqual(agent_hook.local_media(blob, self.tmp), [])
        self.assertLess(time.time() - started, 1.0)
        text = ('https://cdn.example.com/out/1_minji.mp4 1_minji.mp4 결과:' + self.video
                + ' ~/nope.png (out/1_minji.mp4)입니다')
        self.assertEqual(agent_hook.local_media(text, self.tmp), [os.path.realpath(self.video)])
        self.assertEqual(agent_hook.label_of(['x\n  [agent-monitor] name = 공부 예린 \n[agent-monitor] role: host']),
                         {'name': '공부 예린', 'role': 'host'})


class Results(LocalMedia):
    """화면의 내려받기·폴더 열기, 도구가 직접 적은 결과 한 줄."""

    def setUp(self):
        super().setUp()
        self.saved_cfg = dict(server.CFG)
        server.CFG['results_dir'] = os.path.join(self.tmp, 'results')
        self.revealed = []
        self.saved_reveal = server.reveal
        server.reveal = self.revealed.append

    def tearDown(self):
        server.reveal = self.saved_reveal
        server.CFG.clear()
        server.CFG.update(self.saved_cfg)
        super().tearDown()

    def cdn(self):
        """인터넷 결과물 주소 흉내: 이미지 하나를 내주는 작은 서버."""
        import http.server
        png = b'\x89PNG\r\n\x1a\n' + bytes(500)

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', str(len(png)))
                self.end_headers()
                self.wfile.write(png)
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        os.environ['no_proxy'] = os.environ['NO_PROXY'] = '127.0.0.1,localhost'
        return 'http://127.0.0.1:%d/out/hf_20261126_abc.png' % srv.server_address[1], png

    def test_tool_result_line_survives_a_reply_that_is_only_a_path(self):
        base = {'session_id': 'lead', 'cwd': self.tmp}
        self.run_hook(dict(base, hook_event_name='SubagentStart', agent_id='a1', agent_type='reel-host'))
        self.run_hook(dict(base, hook_event_name='PostToolUse', agent_id='a1', tool_name='Bash', tool_response={
            'stdout': '[agent-monitor] text: 재테크 민지 릴스 완성 · 5.2초\n완성: ' + self.video}))
        self.run_hook(dict(base, hook_event_name='SubagentStop', agent_id='a1', last_assistant_message=self.video))
        a = self.agents()['claude:lead:a1']
        self.assertEqual((a['state'], a['text'], len(a['media'])), ('reported', '재테크 민지 릴스 완성 · 5.2초', 1))

    def test_download_a_local_result_as_a_file(self):
        url = self.req('POST', '/api/agent', {'id': 'w', 'state': 'waiting', 'media_url': self.video})[1]['agent']['media'][0]
        from urllib.parse import quote
        status, h, body = self.raw('GET', '/api/download?url=' + quote(url))
        self.assertEqual((status, body), (200, self.data))
        self.assertIn("filename*=UTF-8''1_minji.mp4", h['Content-Disposition'])
        self.assertEqual(self.raw('GET', '/api/download?url=' + quote('/media/0123456789abcdef0123.mp4'))[0], 404)

    def test_internet_result_is_saved_to_the_results_folder_and_revealed(self):
        url, png = self.cdn()
        from urllib.parse import quote
        self.assertEqual(self.raw('GET', '/api/download?url=' + quote(url))[0], 404)   # 화면에 없는 주소는 받지 않는다
        self.req('POST', '/api/agent', {'id': 'img', 'state': 'waiting', 'media_url': url})
        status, h, body = self.raw('GET', '/api/download?url=' + quote(url))
        self.assertEqual((status, body), (200, png))
        saved = os.path.join(self.tmp, 'results', 'hf_20261126_abc.png')
        self.assertTrue(os.path.isfile(saved))
        status, body = self.req('POST', '/api/reveal', {'url': url})
        self.assertEqual((status, body['ok']), (200, True))
        self.assertEqual(self.revealed, [saved])
        self.assertTrue(self.req('GET', '/api/client')[1]['local'])

    def test_same_file_name_from_another_result_is_not_overwritten(self):
        url, png = self.cdn()
        from urllib.parse import quote
        other = url.replace('/out/', '/other/')                     # 이름은 같고 주소는 다른 결과물
        self.req('POST', '/api/agent', {'id': 'a', 'state': 'waiting', 'media_url': url})
        self.req('POST', '/api/agent', {'id': 'b', 'state': 'waiting', 'media_url': other})
        first = self.req('POST', '/api/reveal', {'url': url})[1]['path']
        again = self.req('POST', '/api/reveal', {'url': url})[1]['path']
        second = self.req('POST', '/api/reveal', {'url': other})[1]['path']
        self.assertEqual(first, again)
        self.assertNotEqual(first, second)
        self.assertEqual(sorted(os.listdir(os.path.join(self.tmp, 'results'))), ['hf_20261126_abc-2.png', 'hf_20261126_abc.png'])


class BackgroundSubagents(LocalMedia):
    """백그라운드 서브에이전트가 끝날 때마다 Claude Code 가 메인 세션에 알림을 넣는 경우."""

    def test_completion_notices_do_not_clear_finished_results(self):
        base = {'session_id': 'lead', 'cwd': self.tmp}
        self.run_hook(dict(base, hook_event_name='UserPromptSubmit', prompt='/reels'))
        for n in (1, 2):
            self.run_hook(dict(base, hook_event_name='SubagentStart', agent_id='a%d' % n, agent_type='reel-host'))
        self.run_hook(dict(base, hook_event_name='PostToolUse', agent_id='a1', tool_name='Bash',
                           tool_response={'stdout': '완성: ' + self.video}))
        self.run_hook(dict(base, hook_event_name='SubagentStop', agent_id='a1', last_assistant_message='끝'))
        self.run_hook(dict(base, hook_event_name='UserPromptSubmit',
                           prompt='<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>'))
        self.run_hook(dict(base, hook_event_name='SubagentStop', agent_id='a2', last_assistant_message='끝'))
        self.run_hook(dict(base, hook_event_name='UserPromptSubmit', prompt='다음 거 해줘'))   # 사람의 새 명령
        ag = self.agents()
        self.assertIn('claude:lead:a1', ag)                  # 결과물이 걸린 타일은 방금 보고했으면 남는다
        self.assertEqual(ag['claude:lead']['state'], 'working')
        server.STORE.agents['claude:lead:a1']['since'] -= 600
        self.run_hook(dict(base, hook_event_name='UserPromptSubmit', prompt='또 다음'))
        self.assertNotIn('claude:lead:a1', self.agents())    # 오래된 결과는 다음 명령에 치운다


FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fake_supertonic')


@unittest.skipUnless(importlib.util.find_spec('numpy'), 'numpy 가 없으면 건너뜀 (supertonic 을 깔면 같이 깔림)')
class AiVoice(Base):
    """선택 기능인 AI 음성. 실제 모델 대신 가짜 supertonic 으로 서버 쪽 흐름을 확인한다."""

    def setUp(self):
        super().setUp()
        server.VOICE = server.Voice()
        sys.modules.pop('supertonic', None)

    def tearDown(self):
        super().tearDown()
        if FAKE in sys.path:
            sys.path.remove(FAKE)
        sys.modules.pop('supertonic', None)
        os.environ.pop('FAKE_SUPERTONIC_FAIL', None)
        server.VOICE = server.Voice()

    def ready(self):
        sys.path.insert(0, FAKE)
        server.VOICE.start()
        for _ in range(50):
            if server.VOICE.state != 'loading':
                break
            time.sleep(0.05)
        return server.VOICE.state

    def get(self, path):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('GET', path)
        r = conn.getresponse()
        body = r.read()
        ctype = r.getheader('Content-Type')
        conn.close()
        return r.status, ctype, body

    def test_not_installed(self):
        server.VOICE.start()
        self.assertEqual(server.VOICE.state, 'missing')
        status, _, body = self.get('/api/tts?text=hi&voice=F1')
        self.assertEqual(status, 503)
        self.assertEqual(self.req('GET', '/api/state')[1]['voice']['state'], 'missing')

    def test_speaks_wav_and_caches(self):
        import urllib.parse
        import wave
        self.assertEqual(self.ready(), 'ready')
        self.assertEqual(server.VOICE.voices[:2], ['F1', 'F2'])           # 정렬된 목록
        fake = sys.modules['supertonic']
        fake.CALLS.clear()
        q = '/api/tts?' + urllib.parse.urlencode({'text': '다 했다니깐요?', 'voice': 'F2', 'speed': '1.12'})
        status, ctype, body = self.get(q)
        self.assertEqual((status, ctype), (200, 'audio/wav'))
        with wave.open(io.BytesIO(body)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (1, 2, 24000))
            self.assertGreater(w.getnframes(), 1000)
        self.assertEqual(self.get(q)[0], 200)
        self.assertEqual(len(fake.CALLS), 1)                              # 같은 문장은 다시 만들지 않는다
        self.assertEqual(fake.CALLS[0][1:], ('F2', 1.12, 'ko'))
        self.get('/api/tts?' + urllib.parse.urlencode({'text': 'All done.', 'voice': 'M1', 'speed': '9'}))
        self.assertEqual(fake.CALLS[1][1:], ('M1', 1.5, 'en'))           # 속도는 범위 안으로

    def test_bad_requests(self):
        self.ready()
        self.assertEqual(self.get('/api/tts?text=hi&voice=Z9')[0], 400)
        self.assertEqual(self.get('/api/tts?text=%20&voice=F1')[0], 400)

    def test_load_failure_falls_back(self):
        os.environ['FAKE_SUPERTONIC_FAIL'] = '1'
        self.assertEqual(self.ready(), 'error')
        self.assertIn('download failed', server.VOICE.error)
        self.assertEqual(self.get('/api/tts?text=hi&voice=F1')[0], 503)

    def test_voice_state_in_snapshot_and_events(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        conn.request('GET', '/events')
        r = conn.getresponse()

        def next_event():
            while True:
                line = r.fp.readline().decode()
                if line.startswith('data: '):
                    return json.loads(line[6:])

        self.assertEqual(next_event()['voice']['state'], 'off')
        sys.path.insert(0, FAKE)
        server.VOICE.start(on_change=server.STORE.voice_changed)
        evt = next_event()
        self.assertEqual((evt['type'], evt['voice']['state']), ('voice', 'ready'))
        conn.close()


if __name__ == '__main__':
    unittest.main()
