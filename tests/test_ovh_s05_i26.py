"""S05-I26: bounded read pages, whole-file hashes and real route budgets."""
import hashlib
import json

import pytest

from test_agent_local_tools import local as local
from test_agent_runtime import Catalog, assistant, make_runtime
from test_agent_codex_loop import WorkstationTools
from rynmesh.agent_runtime import local_tools, local_specs, results
from rynmesh.agent_runtime.store import AgentToolRejected


def read(local, args, context=None):
    tools, original, _, _ = local
    return tools.perform('ryn_file_read', args, context or original, lambda: False)


def test_t1_twenty_mib_log_pages_to_eof_with_whole_file_hash(local, monkeypatch):
    tools, context, work, state = local
    unit = b'log entry ' + b'x' * 111 + b'\n'
    body = (unit * ((20 * 1024 * 1024) // len(unit) + 1))[:20 * 1024 * 1024]
    path = work / 'large.log'
    path.write_bytes(body)
    expected = hashlib.sha256(body).hexdigest()
    original = local_tools.decode_text
    decoded_sizes = []
    def bounded_decode(raw):
        decoded_sizes.append(len(raw))
        assert len(raw) <= 256 * 1024, 'large files must not be decoded as one object'
        return original(raw)
    monkeypatch.setattr(local_tools, 'decode_text', bounded_decode)
    parts, offset = [], 0
    while offset is not None:
        page = read(local, {'path': 'large.log', 'offset': offset}, {**context, 'context_window_chars': 768000})
        assert page['offset_unit'] == 'bytes' and page['total_lines'] is None
        assert page['sha256'] == expected and page['bytes'] == len(body)
        assert 0 < len(page['text']) <= 48000
        parts.append(page['text'].encode())
        next_offset = page['next_offset']
        assert next_offset is None or offset < next_offset <= len(body)
        offset = next_offset
    assert b''.join(parts) == body and decoded_sizes
    saved = state.read()['tasks']['task']
    assert any(value.get('sha256') == expected for value in saved['observed_files'].values())


@pytest.mark.parametrize('encoding,bom', [('utf-8', b''), ('utf-8-sig', b'\xef\xbb\xbf'),
    ('utf-16-le', b'\xff\xfe'), ('utf-16-be', b'\xfe\xff'), ('gb18030', b'')])
def test_t1_large_unicode_pages_preserve_boundaries(local, encoding, bom):
    _, context, work, _ = local
    codec = 'utf-8' if encoding == 'utf-8-sig' else encoding
    line = '日志😀数据，汉字\n'
    text = line * ((9 * 1024 * 1024) // len(line.encode(codec)) + 1)
    body = bom + text.encode(codec)
    (work / 'unicode.log').write_bytes(body)
    first = read(local, {'path': 'unicode.log'}, {**context, 'context_window_chars': 768000})
    second = read(local, {'path': 'unicode.log', 'offset': first['next_offset']}, {**context, 'context_window_chars': 768000})
    assert first['encoding'] == encoding
    assert text.startswith(first['text'] + second['text'])
    assert first['next_offset'] == len(bom) + len(first['text'].encode(codec))
    assert first['sha256'] == second['sha256'] == hashlib.sha256(body).hexdigest()


def test_t1_large_ascii_header_then_gb18030_content(local):
    _, _, work, _ = local
    header = b'a' * (9 * 1024 * 1024)
    tail = '后来出现的中文日志\n' * 10
    body = header + tail.encode('gb18030')
    (work / 'late.log').write_bytes(body)
    page = read(local, {'path': 'late.log', 'offset': len(header)})
    assert page['text'] == tail and page['encoding'] == 'gb18030'
    assert page['next_offset'] is None and page['sha256'] == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize('args', [{}, {'start_line': 2}, {'mode': 'indentation', 'anchor_line': 2}])
def test_t2_long_first_line_is_bounded_and_offset_continues(local, args):
    _, _, work, _ = local
    prefix = 'header\n' if args else ''
    text = prefix + '{"data":"' + 'x' * (1024 * 1024 - 128) + '"}\nlast\n'
    (work / 'one.json').write_text(text, encoding='utf-8', newline='')
    page = read(local, {'path': 'one.json', **args})
    assert len(page['text']) <= 12000 and page['line_truncated'] is True
    assert page['next_offset'] == len(prefix) + len(page['text'])
    second = read(local, {'path': 'one.json', 'offset': page['next_offset']})
    assert second['text'] == text[page['next_offset']:page['next_offset'] + 12000]


@pytest.mark.parametrize('window,length', [(None, 12000), (80000, 12000), (320000, 20000), (768000, 48000), (2000000, 48000)])
def test_t3_page_length_is_clamped_to_route_window(local, window, length):
    _, context, work, _ = local
    (work / 'line.txt').write_text('x' * 80000, encoding='utf-8')
    if window is not None:
        context = {**context, 'context_window_chars': window}
    page = read(local, {'path': 'line.txt'}, context)
    assert len(page['text']) == length


def test_t3_real_runtime_dispatch_passes_selected_model_window_and_retains_page(tmp_path, monkeypatch):
    work = tmp_path / 'work'
    work.mkdir()
    (work / 'source.txt').write_text('x' * 80000, encoding='utf-8')
    runtime, model, _, _ = make_runtime(tmp_path / 'node', assistant(name='ryn_file_read', args={'path': 'source.txt'}))
    class LargeCatalog(Catalog):
        def list_models(self):
            return [{**super().list_models()[0], 'context_window': 262144,
                     'context_window_known': True, 'context_window_basis': 'test_catalog'}]
    runtime.catalog = LargeCatalog()
    tools = WorkstationTools(runtime, tmp_path / 'node')
    runtime.tools = tools
    runtime.update_settings({'workspace_root': str(work), 'permission_mode': 'workspace'})
    seen = []
    original = tools.local.perform
    def observe(name, args, context, cancelled, *extra, **kwargs):
        result = original(name, args, context, cancelled, *extra, **kwargs)
        if name == 'ryn_file_read':
            seen.append((context, result))
        return result
    monkeypatch.setattr(tools.local, 'perform', observe)
    try:
        row = runtime.create({'message': 'Read source.'})
        runtime.run_once()
        runtime.run_once()
        assert seen[0][0]['context_window_chars'] >= 600000  # the selected 256K window, not a small default
        assert len(seen[0][1]['text']) == 48000
        saved = runtime.get(row['id'])
        reference = saved['result_refs'][0]
        page = runtime.get_result(row['id'], reference['id'], 0, 48000)
        assert len(page['content']) == 48000 and results.MAX_RESULT_BYTES >= 48000
    finally:
        tools.local.processes.close()


@pytest.mark.parametrize('tool,args', [('ryn_file_write', {'path': 'two.txt', 'content': 'replacement'}),
    ('ryn_file_edit', {'path': 'two.txt', 'old_text': 'x', 'new_text': 'y'})])
def test_t4_writes_and_edits_still_reject_two_mib(local, tool, args):
    tools, context, work, _ = local
    before = b'x' * (2 * 1024 * 1024)
    (work / 'two.txt').write_bytes(before)
    with pytest.raises(AgentToolRejected, match='agent_local_file_missing_or_too_large|agent_local_file_too_large'):
        tools.preview(tool, args, context)
    assert (work / 'two.txt').read_bytes() == before


def test_t1_read_two_mib_and_existing_table_source_use_larger_read_cap(local):
    tools, context, work, _ = local
    body = b'x' * (2 * 1024 * 1024)
    path = work / 'two.txt'
    path.write_bytes(body)
    assert len(read(local, {'path': 'two.txt'})['text']) == 12000
    assert tools.read_window(path) == body
    recipe = {'columns': [{'name': 'item', 'type': 'text'}, {'name': 'amount', 'type': 'decimal'}]}
    raw = b'item,amount\n' + ((b'i' * 800 + b',1\n') * 3000)
    (work / 'table.csv').write_bytes(raw)
    report = tools.perform('ryn_table_calculate', {'source_path': 'table.csv', 'recipe': recipe}, context, lambda: False)
    assert report['status'] == 'passed'


def test_t1_read_cap_is_sixty_four_mib(local):
    _, _, work, _ = local
    with (work / 'oversize.log').open('wb') as stream:
        stream.seek(64 * 1024 * 1024)
        stream.write(b'x')
    with pytest.raises(AgentToolRejected, match='agent_local_file_missing_or_too_large|agent_local_file_too_large'):
        read(local, {'path': 'oversize.log'})


@pytest.mark.parametrize('args', [{'start_line': 1}, {'mode': 'indentation', 'anchor_line': 1}])
def test_t1_large_files_explicitly_require_byte_offset_pages(local, args):
    _, _, work, _ = local
    (work / 'offset.log').write_bytes(b'line\n' * (2 * 1024 * 1024))
    with pytest.raises(AgentToolRejected, match='agent_local_large_file_requires_offset') as caught:
        read(local, {'path': 'offset.log', **args})
    assert 'offset=0' in caught.value.correction and 'next_offset' in caught.value.correction


def test_t3_description_reports_read_cap_and_adaptive_page():
    description = local_specs.SPECS['ryn_file_read'][0]
    assert '64 MiB' in description and '48000' in description and '12000' in description
    assert 'up to 1 MiB' not in description
