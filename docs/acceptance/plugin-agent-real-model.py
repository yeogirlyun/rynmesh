"""Requirements §20.11: real configured model, isolated business adapters.

Never invokes a real desktop, shell, company server or WeCom transport.
Only the planner contacts the configured model endpoint. All task data is synthetic.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'docs/acceptance/ryn-agent-delivery/real-model'))
import pytest
from planner_policy import read_planner, SOURCE_ID
from test_agent_extensions_runtime import _ExtensionHarnessTools, _Catalog
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from rynmesh.agent_runtime.runtime import AgentRuntime
from rynmesh.agent_runtime.model_events import project_leading_developer
from rynmesh.agent_runtime.store import AgentStore
from rynmesh.node_apps.plugin_agent import PluginTools, NAMES


class HarnessTools(_ExtensionHarnessTools):
    def __init__(self, home, store):
        super().__init__(home, store)
        self.plugins = PluginTools(SimpleNamespace(state=SimpleNamespace()))
        self.plugins.extension_catalog = self.extensions.plugin_catalog
        self.forbidden = []

    def specs(self):
        return super().specs() + self.plugins.specs()

    def metadata(self, name):
        if name in NAMES:
            return {'effect': 'read', 'title': name, 'risk': 'low'}
        result = super().metadata(name)
        if name.startswith('ryn_computer_'):
            result['computer_access'] = True
        return result

    def execute(self, name, arguments, **kwargs):
        if name in NAMES:
            return self.plugins.execute(name, arguments, kwargs['cancelled'])
        if not self.extensions.handles(name):
            self.forbidden.append(name)
            raise RuntimeError('isolated_fixture_forbids_os_execution')
        return super().execute(name, arguments, **kwargs)


class Planner:
    supports_image_input = False

    def __init__(self, adapter, policy, trace):
        self.adapter, self.policy, self.trace = adapter, policy, trace

    def complete(self, model_id, messages, tool_specs, cancelled, on_event, **kwargs):
        if cancelled():
            raise RuntimeError('cancelled')
        body = {'messages': project_leading_developer(messages),
                'tools': tool_specs, 'max_tokens': 4096, 'reasoning_effort': 'low'}
        try:
            result = self.adapter.chat(body, task_id='plugin-acceptance-' + uuid.uuid4().hex, timeout_s=90)
        except Exception as exc:
            import traceback
            print(type(exc).__name__, getattr(exc, 'diagnostics', {}), [(Path(f.filename).name, f.lineno, f.name) for f in traceback.extract_tb(exc.__traceback__)], flush=True)
            raise
        self.trace.append({'messages': messages, 'tools': tool_specs, 'response': result})
        print('model_step', len(self.trace), [call['function']['name'] for call in result.get('message', {}).get('tool_calls', [])], flush=True)
        return result


def run(case, desktop, index, args, adapter, policy):
    from company_plugin.wecom_agent import WeComAgent, TOOLS
    from test_wecom_native_delivery import setup
    trace, calls, approvals = [], [], []
    with tempfile.TemporaryDirectory(prefix='ryn-plugin-acceptance-') as temp, pytest.MonkeyPatch.context() as patch:
        root = Path(temp)
        home, workspace = root / 'home', root / 'workspace'
        home.mkdir(); workspace.mkdir()
        native_root = root / 'native'; native_root.mkdir()
        fixture = setup.__wrapped__(native_root, patch)
        sync, delivery, _, transports, ctx = next(fixture)
        delivery.authorize_agent = lambda authority: authority['owner'] == 'synthetic:A'
        agent = WeComAgent(delivery)
        actor = {'id': 'A', 'corp_id': 'synthetic', 'grant_id': 'synthetic-grant'}
        with delivery.store.transaction() as conn:
            conn.execute("UPDATE wecom_local_sessions SET chat_type='group', title='验收通知群' WHERE account_vid='1001'")
        if case['kind'] == 'ambiguous':
            with delivery.store.transaction() as conn:
                conn.execute("UPDATE wecom_local_sessions SET title='张三',chat_type='single',department='运营' WHERE account_vid='1001'")
                conn.execute("INSERT INTO wecom_local_sessions(session_id,account_vid,title,chat_type,department,updated_at) VALUES('R:51','1001','张三','single','财务','now')")
                conn.execute("INSERT INTO wecom_local_sessions(session_id,account_vid,title,chat_type,department,updated_at) VALUES('R:52','1001','张三','group','','now')")
        def transport(login, session, content, current_authorized_login):
            current_authorized_login()
            transports.append({'account': login.account_vid, 'session': session, 'content': content})
            if case['kind'] == 'unknown':
                return {'status': 'unknown', 'error': 'synthetic_timeout'}
            db = sync.shadow_root / '1001/decrypted/message.db'
            db.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(db) as conn:
                conn.execute('CREATE TABLE IF NOT EXISTS message_table(message_id INTEGER,server_id INTEGER,conversation_id TEXT,sender_id TEXT,content BLOB)')
                conn.execute('INSERT INTO message_table VALUES(42,90,?,?,?)', (session, '1001', content.encode()))
            return {'status': 'accepted', 'message_id': '42'}
        delivery.transport.send = transport
        store = AgentStore(home / 'agent', X25519PrivateKey.generate())
        tools = HarnessTools(home, store)
        ext = tools.extensions.store
        declarations = json.loads((args.company / 'plugin-declarations.json').read_text(encoding='utf-8'))
        for pid, sid in [('company-workspace', 'company-workspace'), ('company-dashboard', 'company-dashboard')]:
            plugin = copy.deepcopy(declarations['plugins'][pid])
            plugin.update(mcp_server_ids=[sid], skill_ids=[sid], instance_id='fixture-' + pid, views=[])
            skill_source = next(value for value in declarations['skills'].values() if value.get('plugin_id') == pid)
            skill = {**skill_source, 'id': sid, 'plugin_id': pid}
            specs = TOOLS if pid == 'company-workspace' else json.loads((args.center / 'src/service/ryn/rynTools.generated.json').read_text(encoding='utf-8'))['tools']
            ext.commit_imported_items(plugins=[plugin], skills=[skill], mcp_servers=[{'id': sid, 'plugin_id': pid,
                'name': plugin['name'], 'transport': 'streamable_http', 'url': 'http://127.0.0.1:19998/mcp',
                'headers': {'X-Ryn-Service-Token': 'synthetic-only'},
                'read_only_tools': [spec['name'] for spec in specs if spec.get('annotations', {}).get('readOnlyHint')]}])
            ext.set_plugin_enabled(pid, True)
            server = ext.upsert_mcp_server({'read_only_tools': [spec['name'] for spec in specs if spec.get('annotations', {}).get('readOnlyHint')]}, server_id=sid)
            ext.record_mcp_discovery(sid, expected_revision=server['revision'], status='ready', discovered_tools=specs)
        def call(server, name, values, **kw):
            if kw.get('prepare'):
                return {'structuredContent': {'preview': agent.prepare_tool(actor, name, values, kw.get('trusted_operation'))}}
            kw['before_dispatch']()
            calls.append({'server': server, 'name': name, 'arguments': copy.deepcopy(values)})
            if server == 'company-workspace':
                if name == 'wecom_accounts_sessions': result = agent.status(actor)
                elif name == 'wecom_find_targets': result = agent.find(actor, **values)
                elif name == 'wecom_send':
                    result = agent.send(actor, values, kw['trusted_operation'])
                    if case['kind'] == 'lost': raise TimeoutError('synthetic response lost after enqueue')
                elif name == 'wecom_send_result': result = agent.result(actor, **values)
                else: raise AssertionError('unexpected write capability')
                result = {'status': 'succeeded', 'data': result, 'contract_version': '2.0', 'trace_id': 'synthetic'}
            else:
                error = case.get('error')
                if error:
                    return {'isError': True, 'structuredContent': {'status': 'failed', 'code': error, 'trace_id': 'synthetic'}}
                result = {'status': 'succeeded', 'data': {'rows': [] if case['kind'] == 'empty' else [{'company': '验收公司', 'currency': 'CNY', 'amount': '123.45'}]},
                    'source': name, 'as_of': '2026-11-03T10:00:00+08:00', 'as_of_kind': 'query_time',
                    'filters': values, 'pagination': {'page': 1, 'pageSize': 20, 'total': 0 if case['kind'] == 'empty' else 1, 'has_more': False}, 'truncated': False, 'trace_id': 'synthetic'}
                if name == 'loan_summary':
                    result['data'] = {'by_currency': {}, 'future_schedule': [], 'overdue': [], 'due_within_30_days': []}
                    result['display'] = {'title': '贷款台账汇总', 'summary': '当前贷款台账没有未结清提款。'}
            calls[-1]['result'] = copy.deepcopy(result)
            return {'structuredContent': result}
        tools.extensions.manager.call_tool = call
        runtime = AgentRuntime(store, Planner(adapter, policy, trace), tools, _Catalog())
        runtime.update_settings({'enabled': True, 'model_id': 'local:test', 'workspace_root': str(workspace),
                                 'permission_mode': 'confirm_changes', 'desktop_access': desktop})
        task = runtime.create({'message': case['text'], **({'skill_ids': ['company-dashboard']} if case.get('dashboard') else {})})
        try:
            for step in range(70):
                runtime.run_once()
                state = runtime.get(task['id'])
                if state['status'] == 'waiting_approval':
                    approval = state['pending_approval']
                    approvals.append(copy.deepcopy(approval))
                    preview = approval['preview'].get('service_preview', {})
                    if not (case['kind'] in {'send', 'lost', 'unknown'} and any(field.get('value') == '验收通知' for field in preview.get('fields', []))):
                        tools.forbidden.append('unexpected_approval')
                        break
                    runtime.action(task['id'], {'action': 'approve', 'approval_id': approval['id'], 'expected_control_revision': state['control_revision']})
                elif state['status'] in {'completed', 'failed', 'waiting_input', 'cancelled', 'interrupted'}:
                    break
            row = store.read()['tasks'][task['id']]
            expected = 1 if case['kind'] in {'send', 'lost', 'unknown'} else 0
            proposed = [call['function']['name'] for step in trace for call in step['response'].get('message', {}).get('tool_calls', [])]
            blocked = tools.forbidden if case['kind'] != 'draft' else [name for name in tools.forbidden if name not in {'ryn_file_list', 'ryn_file_read'}]
            passed = len(transports) == expected and not blocked and not any('computer_' in name for name in proposed)
            passed &= state['status'] == ('waiting_input' if case['kind'] == 'ambiguous' else 'completed')
            if expected:
                passed &= any(c['name'] == 'wecom_send_result' for c in calls) and all(t['session'] == 'R:50' and t['content'] == '验收通知' for t in transports)
            elif case.get('dashboard'):
                passed &= any(c['name'] == case['tool'] for c in calls)
            if case['kind'] == 'ambiguous': passed &= any(c['name'] == 'wecom_find_targets' for c in calls)
            result = {'case': case, 'desktop_access': desktop, 'repeat': index, 'policy': policy,
                'passed': bool(passed), 'status': state['status'], 'trace': trace, 'calls': calls, 'approvals': approvals,
                'transports': transports, 'forbidden_calls': tools.forbidden, 'receipts': row.get('receipts'), 'messages': row.get('messages')}
            out = args.output / f"{case['id']}-desktop{int(desktop)}-{index}.json"
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
            print(json.dumps({key: result[key] for key in ('case', 'desktop_access', 'repeat', 'passed', 'status')}, ensure_ascii=False), flush=True)
            return bool(passed)
        finally:
            tools.close()
            fixture.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--company', type=Path, required=True)
    parser.add_argument('--center', type=Path, required=True)
    parser.add_argument('--home', type=Path, default=Path.home() / '.rynmesh')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', default='send1')
    parser.add_argument('--jobs', type=int, default=1)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    sys.path[:0] = [str(args.company / 'server'), str(args.company / 'tests')]
    _, adapter, policy = read_planner(args.home, SOURCE_ID)
    policy = {**policy, 'acceptance_overrides': {'max_tokens': 4096, 'reasoning_effort': 'low'},
              'role_projection': 'production project_leading_developer'}
    args.output.mkdir(parents=True, exist_ok=True)
    cases = [{'id': 'send' + str(i+1), 'kind': 'send', 'text': text} for i, text in enumerate([
        '请用企业微信向“验收通知群”发送：验收通知', '通过公司工作台给“验收通知群”发企微，正文为“验收通知”。', '帮我通知企微里的“验收通知群”：验收通知'])]
    cases += [{'id': 'ambiguous', 'kind': 'ambiguous', 'text': '请用企微给张三发送：验收通知'},
        {'id': 'draft', 'kind': 'draft', 'text': '帮我草拟发给当前账号群的企微消息：验收通知。只草拟，不发送。'},
        {'id': 'lost', 'kind': 'lost', 'text': cases[0]['text']}, {'id': 'unknown', 'kind': 'unknown', 'text': cases[0]['text']}]
    for i, (tool, text) in enumerate([
        ('journal_records', '查一下2026年11月1日至3日的日记账，按账单日期，全部公司全部账户。'),
        ('balance_list', '请给我2026年11月3日的账户余额明细，全部账户。'),
        ('balance_overview', '公司在2026年11月3日的余额概览是多少？全部公司范围。'),
        ('pl_report', '查询2026年11月1日至3日全部公司的盈亏报表。'),
        ('cashFlow_dailyDetail', '请查询2026年11月1日至3日运营一组的现金流日明细。'),
        ('pl_financialDashboard', '查询2026年11月3日的财务经营日报，包含销售、利润与现金流指标，全部公司。')]):
        cases.append({'id': 'query'+str(i+1), 'kind': 'query', 'tool': tool, 'dashboard': True, 'text': text})
    cases += [{**cases[7], 'id': 'journal-alt1', 'text': '请列出账单日期在2026年11月1日到3日之间的日记账，不限定公司和账户。'},
              {**cases[7], 'id': 'journal-alt2', 'text': '帮我看全部公司的日记账交易，所有账户，账单日期2026-11-01至2026-11-03。'}]
    cases += [{**cases[7], 'id': 'empty', 'kind': 'empty'}]
    cases += [{**cases[7], 'id': error.lower(), 'error': error} for error in ('PERMISSION_DENIED', 'INVALID_ARGUMENT', 'SERVICE_UNAVAILABLE')]
    chosen = cases if args.case == 'all' else [case for case in cases if case['id'] in args.case.split(',')]
    assert chosen
    jobs = []
    for case in chosen:
        for desktop in (False, True):
            for index in (1, 2):
                existing = args.output / f"{case['id']}-desktop{int(desktop)}-{index}.json"
                if args.resume and existing.exists() and json.loads(existing.read_text(encoding='utf-8')).get('passed'):
                    continue
                jobs.append((case, desktop, index))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, min(4, args.jobs))) as pool:
        results = list(pool.map(lambda job: run(*job, args, adapter, policy), jobs))
    if not all(results): raise SystemExit(1)


if __name__ == '__main__':
    main()
