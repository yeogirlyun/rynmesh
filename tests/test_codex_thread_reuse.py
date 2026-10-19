"""Codex tool loops continue one private thread; anything uncertain starts over."""
import copy
import io
import json
import queue
import threading
import time
from collections import deque
from types import SimpleNamespace

import pytest
from test_agent_runtime import advance, make_runtime

from rynmesh.agent_runtime.bridge import AgentModelBridge
from rynmesh.agent_runtime.models import ModelCatalog
from rynmesh.llm_package import cli_adapter, codex_session, codex_threads
from rynmesh.llm_package.adapters import AdapterError
from rynmesh.llm_package.cli_adapter import CLIAgentAdapter
from rynmesh.llm_package.codex_tools import ContentStream

PARAMETERS = {"type": "object", "properties": {"path": {"type": "string"}},
              "required": ["path"], "additionalProperties": False}
TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read at origin", "parameters": PARAMETERS}}]
LIST = {"type": "function", "function": {"name": "list_dir", "description": "List at origin", "parameters": PARAMETERS}}
SYSTEM = 'Agent rules.\nServer control state: {"plan_revision":1,"receipts":[]}\nOwner corrections steer the task.'


class Process:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code

    def kill(self):
        self.code = -9

    def wait(self, timeout=None):
        return self.code


class Codex:
    """Scripted app-server: each turn pops the next planned protocol object."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.instances = []
        self.replies = deque()
        self.cpu = None  # None: no lifetime CPU limit, as on Windows

    def factory(self):
        codex = self

        class RPC:
            def __init__(self, *args):
                self.args = args
                self.root, self.process = codex.tmp_path, Process()
                self.pending, self.calls, self.rejections, self.deltas = deque(), [], [], []
                self.closed = self.stream = False
                codex.instances.append(self)

            def initialize(self):
                pass

            def call(self, method, params):
                self.calls.append((method, copy.deepcopy(params)))
                if method == "thread/start":
                    return {"thread": {"id": f"thread-{len(codex.instances)}"}}
                thread, turn = params["threadId"], f"turn-{len(self.calls)}"
                reply = codex.replies.popleft()
                if callable(reply):
                    reply = reply(self, params, turn)
                if reply is not None:
                    text = json.dumps(reply, ensure_ascii=False)
                    if self.stream:
                        self.deltas.extend({"threadId": thread, "turnId": turn, "itemId": "item", "delta": text[i:i + 7]}
                                           for i in range(0, len(text), 7))
                        self.pending.append(codex_session.DELTA_SIGNAL)
                    self.pending.extend([
                        {"method": "item/completed", "params": {"threadId": thread, "turnId": turn,
                            "item": {"type": "agentMessage", "text": text}}},
                        {"method": "thread/tokenUsage/updated", "params": {"threadId": thread, "turnId": turn,
                            "tokenUsage": {"last": {"inputTokens": 11, "outputTokens": 3}}}},
                        {"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "completed"}}}])
                return {"turn": {"id": turn}}

            def receive(self):
                # Nothing left to read: what a killed or crashed app-server looks like.
                raise AdapterError("Codex app-server disconnected", code="runtime_connection_failed")

            def diagnostics(self):
                return {"rpc_stage": self.stage, "session_mode": self.session_mode,
                        "events_received": self.events_received, "deltas_received": self.deltas_received,
                        "last_event_age_seconds": None, "process_alive": self.process.poll() is None}

            def take_deltas(self):
                deltas, self.deltas = self.deltas, []
                return deltas

            def reject(self, value):
                self.rejections.append(value)

            def alive(self):
                return not self.closed and self.process.poll() is None

            def cpu_seconds(self, estimate):
                return codex.cpu

            def settle(self):
                return True

            def close(self):
                self.closed = True
                self.process.kill()

        return RPC

    @staticmethod
    def turns(instance):
        return [params for method, params in instance.calls if method == "turn/start"]

    @staticmethod
    def payload(params):
        return json.loads(params["input"][0]["text"].split("\n", 1)[1])


@pytest.fixture
def codex(monkeypatch, tmp_path):
    fake = Codex(tmp_path)
    monkeypatch.setattr(codex_session, "CodexRPC", fake.factory())
    monkeypatch.setattr(CLIAgentAdapter, "_command_path", lambda _: "fixture-codex")
    return fake


def propose(name="read_file", **arguments):
    return {"content": "", "tool_calls": [{"name": name, "arguments": arguments}]}


def answer(text):
    return {"content": text, "tool_calls": []}


def step(adapter, messages, task_id, tools=TOOLS, on_event=None, scope=None, model=""):
    """One origin-client loop step: infer, then append the call and its executed result."""
    body = {"messages": messages, "tools": tools, "max_tokens": 256, **({"stream": True} if on_event else {})}
    result = adapter.chat(copy.deepcopy(body), task_id=task_id, timeout_s=5, on_event=on_event,
                          conversation_scope=scope, model=model)
    messages.append(copy.deepcopy(result["message"]))
    for call in result["message"].get("tool_calls", []):
        path = json.loads(call["function"]["arguments"])["path"]
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": "contents of " + path})
    return result


def conversation():
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "read a and b"}]


def test_tool_loop_reuses_one_process_and_thread_and_sends_only_new_messages(codex):
    codex.replies.extend([propose(path="a.txt"), propose(path="b.txt"), answer("both read")])
    adapter, messages = CLIAgentAdapter("codex_cli"), conversation()
    step(adapter, messages, "s1")
    step(adapter, messages, "s2")
    final = step(adapter, messages, "s3")
    assert final["text"] == "both read" and final["finish_reason"] == "stop"
    assert len(codex.instances) == 1
    rpc = codex.instances[0]
    assert [method for method, _ in rpc.calls] == ["thread/start", "turn/start", "turn/start", "turn/start"]
    thread = rpc.calls[0][1]
    assert thread["ephemeral"] is True and thread["environments"] == [] and thread["dynamicTools"] == []
    assert thread["sandbox"] == "read-only" and thread["approvalPolicy"] == "never"
    assert thread["runtimeWorkspaceRoots"] == [] and thread["selectedCapabilityRoots"] == []
    seed, second, third = codex.turns(rpc)
    # Functions and the schema precede the growing transcript.
    assert list(codex.payload(seed)) == ["functions", "tool_choice", "output_schema", "messages"]
    assert codex.payload(seed)["messages"] == messages[:2]
    assert codex.payload(second) == {"tool_choice": "auto", "messages": [messages[3]]}
    assert codex.payload(third) == {"tool_choice": "auto", "messages": [messages[5]]}
    for params in (seed, second, third):
        assert params["threadId"] == "thread-1" and params["environments"] == [] and "outputSchema" in params
    for params in (second, third):
        assert SYSTEM not in params["input"][0]["text"] and "read a and b" not in params["input"][0]["text"]
    pool = codex_threads.pool_for(adapter)
    assert rpc.closed and len(pool) == 0  # a final answer ends the tool loop
    assert pool.stats == {"started": 1, "resumed": 2, "fallback": 0, "rebuilt": 0}
    assert adapter._running == {} and adapter._cancelled == set()


def test_server_state_updates_in_place_but_policy_prose_change_starts_a_new_thread(codex):
    codex.replies.extend([propose(path="a.txt"), propose(path="b.txt"), propose(path="c.txt")])
    adapter, messages = CLIAgentAdapter("codex_cli"), conversation()
    step(adapter, messages, "s1")
    messages[0] = {"role": "system", "content": SYSTEM.replace('"receipts":[]', '"receipts":["op_1"]')}
    step(adapter, messages, "s2")
    assert len(codex.instances) == 1
    update = codex.payload(codex.turns(codex.instances[0])[1])
    assert update["system_state_updates"] == ['Server control state: {"plan_revision":1,"receipts":["op_1"]}']
    assert update["messages"] == [messages[3]]
    # Permission policy is prose: a changed rule never rides along in a kept thread.
    messages[0] = {"role": "system", "content": messages[0]["content"].replace("Agent rules.", "Agent rules. This task is read-only.")}
    sent = copy.deepcopy(messages)
    step(adapter, messages, "s3")
    assert len(codex.instances) == 2 and codex.instances[0].closed
    assert codex.payload(codex.turns(codex.instances[1])[0])["messages"] == sent


def test_context_and_function_changes_are_sent_as_updates(codex):
    codex.replies.extend([propose(path="a.txt"), propose("list_dir", path="."), propose("list_dir", path="b")])
    adapter, messages = CLIAgentAdapter("codex_cli"), conversation()
    step(adapter, messages, "s1")
    plan = {"role": "assistant", "content": "Untrusted model plan proposal: read a, then list"}
    messages.insert(1, plan)
    step(adapter, messages, "s2", tools=[*TOOLS, LIST])
    step(adapter, messages, "s3", tools=[LIST])
    assert len(codex.instances) == 1
    _, second, third = codex.turns(codex.instances[0])
    assert codex.payload(second)["context_messages"] == [plan]
    assert codex.payload(second)["functions_update"] == {"added_or_replaced": [LIST["function"]], "removed": []}
    assert codex.payload(third)["functions_update"] == {"added_or_replaced": [], "removed": ["read_file"]}
    # The schema of each turn still admits only the current names.
    assert third["outputSchema"]["properties"]["tool_calls"]["items"]["anyOf"][0]["properties"]["name"]["enum"] == ["list_dir"]


def test_cancel_during_a_continued_turn_closes_the_thread_without_fallback(codex, monkeypatch):
    monkeypatch.setattr(cli_adapter, "terminate", lambda process: process.kill())
    adapter, messages = CLIAgentAdapter("codex_cli"), conversation()

    def cancelled(rpc, params, turn):
        adapter.cancel("s2")  # what the bridge's watcher does when the owner cancels
        return None

    codex.replies.extend([propose(path="a.txt"), cancelled])
    step(adapter, messages, "s1")
    with pytest.raises(AdapterError):
        step(adapter, messages, "s2")
    pool = codex_threads.pool_for(adapter)
    assert len(codex.instances) == 1 and codex.instances[0].closed
    assert codex.instances[0].process.poll() is not None
    assert len(pool) == 0 and pool.stats["fallback"] == 0
    assert adapter._running == {} and adapter._cancelled == set()


def test_crash_in_a_continued_turn_falls_back_to_full_history_once(codex):
    def crash(rpc, params, turn):
        rpc.process.kill()
        return None

    codex.replies.extend([propose(path="a.txt"), crash, propose(path="b.txt")])
    adapter, messages = CLIAgentAdapter("codex_cli"), conversation()
    step(adapter, messages, "s1")
    sent = copy.deepcopy(messages)
    result = step(adapter, messages, "s2")
    assert len(codex.instances) == 2
    kept, fallback = codex.instances
    assert kept.closed and fallback.closed  # the fallback is the one-shot stateless mode
    assert fallback.calls[0][1]["developerInstructions"].startswith("You are a stateless")
    # The full client record already holds the executed tool result; nothing is replayed.
    assert codex.payload(codex.turns(fallback)[0])["messages"] == sent
    earlier = {call["id"] for message in sent for call in message.get("tool_calls", [])}
    assert result["message"]["tool_calls"][0]["id"] not in earlier
    assert codex_threads.pool_for(adapter).stats == {"started": 1, "resumed": 0, "fallback": 1, "rebuilt": 0}
    assert adapter._running == {} and adapter._cancelled == set()


def test_thread_that_exited_while_idle_is_replaced_with_full_history(codex):
    codex.replies.extend([propose(path="a.txt"), propose(path="b.txt")])
    adapter, messages = CLIAgentAdapter("codex_cli"), conversation()
    step(adapter, messages, "s1")
    codex.instances[0].process.kill()
    sent = copy.deepcopy(messages)
    step(adapter, messages, "s2")
    assert len(codex.instances) == 2 and codex.instances[0].closed
    replacement = codex.instances[1]
    assert codex.payload(codex.turns(replacement)[0])["messages"] == sent
    assert replacement.calls[0][1]["developerInstructions"].startswith("You are a reasoning backend for one remote client tool loop")
    assert not replacement.closed and len(codex_threads.pool_for(adapter)) == 1


def test_no_fallback_once_text_was_streamed(codex):
    def partial(rpc, params, turn):
        rpc.deltas.append({"threadId": params["threadId"], "turnId": turn, "itemId": "item", "delta": '{"content":"Reading b'})
        rpc.pending.append(codex_session.DELTA_SIGNAL)
        rpc.process.kill()
        return None

    codex.replies.extend([propose(path="a.txt"), partial])
    adapter, messages, events = CLIAgentAdapter("codex_cli"), conversation(), []
    step(adapter, messages, "s1", on_event=events.append)
    with pytest.raises(AdapterError) as lost:
        step(adapter, messages, "s2", on_event=events.append)
    assert lost.value.code == "runtime_connection_failed"
    assert any(event["choices"][0]["delta"].get("content") == "Reading b" for event in events)
    assert len(codex.instances) == 1 and codex.instances[0].closed


def test_stream_forwards_answer_text_before_tool_proposals(codex):
    content = 'Reading "a.txt" — 中文 😀 now'
    codex.replies.append({"content": content, "tool_calls": [{"name": "read_file", "arguments": json.dumps({"path": "a.txt"})}]})
    adapter, messages, events = CLIAgentAdapter("codex_cli"), conversation(), []
    result = step(adapter, messages, "s1", on_event=events.append)
    chunks = [event["choices"][0]["delta"]["content"] for event in events[:-2]]
    assert len(chunks) > 1 and "".join(chunks) == content == result["message"]["content"]
    assert events[0]["choices"][0]["delta"]["role"] == "assistant"
    proposal = events[-2]["choices"][0]["delta"]
    assert "content" not in proposal and proposal["tool_calls"][0]["function"]["name"] == "read_file"
    assert proposal["tool_calls"][0]["index"] == 0
    assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert events[-1]["usage"] == {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}


@pytest.mark.parametrize("size", [1, 2, 3, 5, 64])
def test_content_stream_decodes_escapes_split_across_deltas(size):
    value = {"content": 'line1\nquote " back\\ tab\t 中文 😀 /', "tool_calls": [{"name": "x", "arguments": "{}"}]}
    for text in (json.dumps(value), json.dumps(value, ensure_ascii=False), json.dumps(value, indent=1)):
        out = []
        stream = ContentStream(out.append)
        for index in range(0, len(text), size):
            stream("item", text[index:index + size])
        assert "".join(out) == value["content"]


@pytest.mark.parametrize("text", ['{"tool_calls": [], "content": "late"}', "plain prose", "[1]", '{"content": 1}'])
def test_content_stream_ignores_other_shapes(text):
    out = []
    ContentStream(out.append)("item", text)
    assert out == []


def test_rejected_proposal_closes_the_thread(codex):
    codex.replies.extend([propose(path="a.txt"), propose("exec_command", path="x"), propose(path="b.txt")])
    adapter, messages = CLIAgentAdapter("codex_cli"), conversation()
    step(adapter, messages, "s1")
    with pytest.raises(AdapterError) as rejected:
        step(adapter, messages, "s2")
    assert rejected.value.code == "upstream_response_invalid"
    assert codex.instances[0].closed
    messages.append({"role": "assistant", "content": "Server note: your previous response was rejected and nothing in it was executed."})
    sent = copy.deepcopy(messages)
    step(adapter, messages, "s3")
    assert len(codex.instances) == 2
    assert codex.payload(codex.turns(codex.instances[1])[0])["messages"] == sent


def test_kept_threads_are_bounded_expire_and_close_on_shutdown(codex, monkeypatch):
    monkeypatch.setattr(codex_threads, "SESSION_LIMIT", 1)
    codex.replies.extend([propose(path="a.txt"), propose(path="b.txt"), propose(path="c.txt")])
    adapter = CLIAgentAdapter("codex_cli")
    first, second = conversation(), [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "other task"}]
    step(adapter, first, "a1")
    step(adapter, second, "b1")
    assert codex.instances[0].closed and not codex.instances[1].closed  # least recently used is evicted
    adapter.shutdown()
    assert codex.instances[1].closed and len(codex_threads.pool_for(adapter)) == 0

    monkeypatch.setattr(codex_threads, "SESSION_IDLE_SECONDS", 0.05)
    other = CLIAgentAdapter("codex_cli")
    step(other, conversation(), "c1")
    until = time.monotonic() + 3
    while not codex.instances[2].closed and time.monotonic() < until:
        time.sleep(0.02)
    assert codex.instances[2].closed and len(codex_threads.pool_for(other)) == 0


def test_reuse_can_be_disabled(codex, monkeypatch):
    monkeypatch.setenv("RYN_CODEX_SESSION_REUSE", "0")
    codex.replies.extend([propose(path="a.txt"), answer("done")])
    adapter, messages = CLIAgentAdapter("codex_cli"), conversation()
    step(adapter, messages, "s1")
    sent = copy.deepcopy(messages)
    step(adapter, messages, "s2")
    assert len(codex.instances) == 2 and all(instance.closed for instance in codex.instances)
    assert codex.payload(codex.turns(codex.instances[1])[0])["messages"] == sent


def test_idle_server_request_voids_the_thread():
    rpc = object.__new__(codex_session.CodexRPC)
    rpc.pending = deque([{"method": "item/completed", "params": {}}])
    rpc.queue = queue.Queue()
    rpc.queue.put({"id": 9, "method": "item/commandExecution/requestApproval", "params": {}})
    rpc.deltas, rpc.delta_lock, sent = [], threading.Lock(), []
    rpc.send = sent.append
    assert rpc.settle() is False
    assert sent == [{"id": 9, "error": {"code": -32601, "message": "Provider execution and interactive approvals are disabled"}}]
    rpc.queue.put({"method": "thread/tokenUsage/updated", "params": {}})
    assert rpc.settle() is True and not rpc.pending and rpc.queue.empty()


def test_streamed_deltas_queue_one_wakeup_and_keep_execution_events():
    rpc = object.__new__(codex_session.CodexRPC)
    delta = {"method": "item/agentMessage/delta", "params": {"threadId": "t", "turnId": "u", "itemId": "i", "delta": "x"}}
    important = [{"id": 2, "method": "item/tool/call", "params": {"tool": "unexpected"}},
                 {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}]
    lines = [delta] * 2048 + important
    rpc.process = SimpleNamespace(stdout=io.BytesIO(b"".join(json.dumps(e).encode() + b"\n" for e in lines)), kill=lambda: None)
    rpc.queue, rpc.closed, rpc.failure = queue.Queue(maxsize=8), threading.Event(), ""
    rpc.deltas, rpc.delta_lock, rpc.stream = [], threading.Lock(), True
    rpc._read()
    assert not rpc.failure
    assert rpc.queue.get_nowait() is codex_session.DELTA_SIGNAL
    assert [rpc.queue.get_nowait() for _ in important] == important
    assert rpc.queue.get_nowait() is None
    assert len(rpc.take_deltas()) == 2048


def test_agent_runtime_steps_continue_the_same_codex_thread(codex, tmp_path):
    from test_agent_reasoning_effort import codex_model

    runtime, _, tools, _ = make_runtime(tmp_path / "agent")
    rows = [codex_model(streaming=True)]
    runtime.catalog.list_models = lambda: copy.deepcopy(rows)
    manager = SimpleNamespace(adapter=CLIAgentAdapter("codex_cli"), manifest=SimpleNamespace(timeout_seconds=60),
                              _slots=threading.BoundedSemaphore(1), _lock=threading.Lock(), _running=0)
    catalog = SimpleNamespace(resolve=lambda _: copy.deepcopy(rows[0]), destination=ModelCatalog.destination)
    runtime.model = AgentModelBridge({"provider_for": lambda _: manager}, catalog)
    codex.replies.extend([propose("inspect_service", service_id="svc1"), propose("inspect_service", service_id="svc2"),
                          answer("Both services inspected.")])
    created = runtime.create({"message": "inspect svc1 and svc2"})
    advance(runtime, 8)
    done = runtime.get(created["id"])
    assert done["status"] == "completed", done.get("error")
    assert done["result"] == "Both services inspected."
    assert [call[:2] for call in tools.calls] == [("inspect_service", {"service_id": "svc1"}),
                                                  ("inspect_service", {"service_id": "svc2"})]
    # The real loop's stored history and system projection extend the thread exactly.
    assert len(codex.instances) == 1
    turns = codex.turns(codex.instances[0])
    assert len(turns) == 3
    for params in turns[1:]:
        (message,) = codex.payload(params)["messages"]
        assert message["role"] == "tool"
    events = runtime.store.read()["tasks"][created["id"]]["events"]
    assert "".join(event["text"] for event in events if event["type"] == "text_delta") == "Both services inspected."


# Window-scoped retention: (command, model, effort, requester, window) groups.

ALICE = codex_threads.scope("peer:alice", codex_threads.window_key("task_alice"))


def window(name, requester="local"):
    return codex_threads.scope(requester, codex_threads.window_key(name))


def test_window_thread_survives_the_final_answer_and_the_next_owner_turn_continues_it(codex):
    codex.replies.extend([propose(path="a.txt"), answer("a read"), propose(path="b.txt"), answer("b read")])
    adapter, messages, scope = CLIAgentAdapter("codex_cli"), conversation(), window("task_1")
    step(adapter, messages, "s1", scope=scope)
    step(adapter, messages, "s2", scope=scope)
    assert not codex.instances[0].closed and len(codex_threads.pool_for(adapter)) == 1
    messages.append({"role": "user", "content": "now read b"})
    step(adapter, messages, "s3", scope=scope)
    step(adapter, messages, "s4", scope=scope)
    assert len(codex.instances) == 1
    turns = codex.turns(codex.instances[0])
    assert codex.payload(turns[2])["messages"] == [{"role": "user", "content": "now read b"}]
    assert codex.payload(turns[3])["messages"] == [messages[-2]]
    assert codex_threads.pool_for(adapter).stats["resumed"] == 3


def test_two_windows_with_the_same_first_message_do_not_supersede_each_other(codex):
    codex.replies.extend([answer("hi A"), answer("hi B"), answer("again A"), answer("again B")])
    adapter = CLIAgentAdapter("codex_cli")
    first = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "你好"}]
    second = copy.deepcopy(first)
    step(adapter, first, "a1", scope=window("task_a"))
    step(adapter, second, "b1", scope=window("task_b"))
    assert len(codex.instances) == 2 and not any(item.closed for item in codex.instances)
    first.append({"role": "user", "content": "A again"})
    second.append({"role": "user", "content": "B again"})
    step(adapter, first, "a2", scope=window("task_a"))
    step(adapter, second, "b2", scope=window("task_b"))
    assert len(codex.instances) == 2
    a, b = (codex.turns(item) for item in codex.instances)
    assert codex.payload(a[1])["messages"] == [{"role": "user", "content": "A again"}]
    assert codex.payload(b[1])["messages"] == [{"role": "user", "content": "B again"}]


def test_another_requester_never_gets_a_thread_even_with_the_identical_transcript(codex):
    codex.replies.extend([propose(path="a.txt"), propose(path="b.txt"), propose(path="c.txt")])
    adapter, messages = CLIAgentAdapter("codex_cli"), conversation()
    step(adapter, messages, "alice-1", scope=ALICE)
    replay = copy.deepcopy(messages)  # same window key and the real call ids
    mallory = codex_threads.scope("peer:mallory", ALICE["window"])
    step(adapter, replay, "mallory-1", scope=mallory)
    assert len(codex.instances) == 2 and not codex.instances[0].closed
    assert codex.payload(codex.turns(codex.instances[1])[0])["messages"] == messages
    step(adapter, messages, "alice-2", scope=ALICE)
    assert len(codex.instances) == 2
    assert len(codex.turns(codex.instances[0])) == 2 and not codex.instances[1].closed


def test_window_threads_expire_after_thirty_idle_minutes(codex):
    assert codex_threads.SESSION_IDLE_SECONDS == 1800
    codex.replies.append(answer("done"))
    adapter = CLIAgentAdapter("codex_cli")
    step(adapter, conversation(), "s1", scope=window("task_1"))
    pool = codex_threads.pool_for(adapter)
    with pool.condition:
        assert not codex.instances[0].closed
        pool.idle[0].idle_since -= 1799
        pool.condition.notify_all()
    time.sleep(0.2)
    assert not codex.instances[0].closed
    with pool.condition:
        pool.idle[0].idle_since -= 2
        pool.condition.notify_all()
    until = time.monotonic() + 3
    while not codex.instances[0].closed and time.monotonic() < until:
        time.sleep(0.02)
    assert codex.instances[0].closed and len(pool) == 0


@pytest.mark.parametrize("scope", [None, codex_threads.scope("local"), codex_threads.scope("local", "not-a-key")])
def test_client_without_a_window_key_falls_back_to_tool_loop_reuse(codex, scope):
    codex.replies.extend([propose(path="a.txt"), answer("a read"), answer("follow-up")])
    adapter, messages = CLIAgentAdapter("codex_cli"), conversation()
    step(adapter, messages, "s1", scope=scope)
    step(adapter, messages, "s2", scope=scope)
    assert len(codex.instances) == 1 and codex.instances[0].closed  # without a window, the final answer ends it
    messages.append({"role": "user", "content": "and then?"})
    sent = copy.deepcopy(messages)
    step(adapter, messages, "s3", scope=scope)
    assert len(codex.instances) == 2
    assert codex.payload(codex.turns(codex.instances[1])[0])["messages"] == sent


def test_model_switch_in_a_window_closes_its_old_thread(codex):
    codex.replies.extend([answer("one"), answer("two")])
    adapter, messages, scope = CLIAgentAdapter("codex_cli"), conversation(), window("task_1")
    step(adapter, messages, "s1", scope=scope, model="gpt-a")
    messages.append({"role": "user", "content": "again"})
    step(adapter, messages, "s2", scope=scope, model="gpt-b")
    assert len(codex.instances) == 2 and codex.instances[0].closed and not codex.instances[1].closed
    assert codex.instances[1].calls[0][1]["model"] == "gpt-b"


def test_cpu_budget_rebuilds_the_thread_before_a_turn_could_cross_it(codex):
    codex.replies.extend([answer("one"), answer("two")])
    adapter, messages, scope = CLIAgentAdapter("codex_cli"), conversation(), window("task_1")
    step(adapter, messages, "s1", scope=scope)
    codex.cpu = codex_threads.SESSION_CPU_SECONDS - 3  # a 5 s turn could cross the POSIX rlimit
    messages.append({"role": "user", "content": "again"})
    sent = copy.deepcopy(messages)
    result = step(adapter, messages, "s2", scope=scope)
    assert result["text"] == "two"
    assert len(codex.instances) == 2 and codex.instances[0].closed
    assert codex.payload(codex.turns(codex.instances[1])[0])["messages"] == sent
    assert codex_threads.pool_for(adapter).stats["rebuilt"] == 1


def test_bridge_sends_only_a_hashed_window_and_the_local_requester(tmp_path):
    from test_agent_reasoning_effort import codex_model

    seen, sent = [], []

    class Adapter:
        supports_conversation_scope = True

        def chat(self, body, **options):
            seen.append(options)
            return {"text": "ok", "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop",
                    "input_tokens": 1, "output_tokens": 1, "duration_ms": 1}

        def cancel(self, task_id):
            return True

    manager = SimpleNamespace(adapter=Adapter(), manifest=SimpleNamespace(timeout_seconds=60),
                              _slots=threading.BoundedSemaphore(1), _lock=threading.Lock(), _running=0)
    local = codex_model()
    bridge = AgentModelBridge({"provider_for": lambda _: manager},
                              SimpleNamespace(resolve=lambda _: copy.deepcopy(local), destination=ModelCatalog.destination))
    bridge.complete(local["id"], [{"role": "user", "content": "hello"}], [], lambda: False, conversation="task_secret")
    assert seen[0]["conversation_scope"] == {"requester": "local", "window": codex_threads.window_key("task_secret")}

    async def execute_order(request):
        sent.append(request)
        return {"state": "succeeded", "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}

    peer = codex_model(id="peer/test", source="peer")
    bridge = AgentModelBridge({"execute_order": execute_order}, SimpleNamespace(
        resolve=lambda _: peer, destination=ModelCatalog.destination, network="fixture"))
    bridge.complete(peer["id"], [{"role": "user", "content": "hello"}], [], lambda: False, conversation="task_secret")
    assert sent[0]["conversation_key"] == codex_threads.window_key("task_secret")
    assert "task_secret" not in json.dumps([seen, sent], default=str)
    bridge.complete(peer["id"], [{"role": "user", "content": "hello"}], [], lambda: False)
    assert "conversation_key" not in sent[1]


def test_provider_derives_the_requester_from_the_signed_sender_not_the_body(codex, tmp_path, monkeypatch):
    from rynmesh.llm_package.manifest import LLMPackageManifest
    from rynmesh.llm_package.routes import ProviderService, _expires
    from rynmesh.llm_package.task_balance import TaskBalanceLedger
    from rynmesh.llm_package.task_protocol import TaskOrderStore, seal_task
    from rynmesh.services import peer_box
    from rynmesh.store import RynmeshStore

    monkeypatch.setattr("rynmesh.llm_package.cli_models.list_models", lambda *args, **kwargs: [
        {"id": "fixture-default", "name": "Authored fixture", "default": True,
         "context_window": 32768, "context_window_known": True, "context_window_basis": "fixture_declared"}])
    a = RynmeshStore(home=tmp_path / "a", network_dir=tmp_path / "net")
    b = RynmeshStore(home=tmp_path / "b", network_dir=tmp_path / "net")
    ak = peer_box.load_or_create_messaging_key(tmp_path / "ak")
    bk = peer_box.load_or_create_messaging_key(tmp_path / "bk")
    b.personal_space = SimpleNamespace(enforces_ai=lambda: True, allows_ai=lambda _: True)
    manifest = LLMPackageManifest(package_id="codex-cli", mode="codex_cli", adapter="codex_cli",
                                  public_model_alias="Codex CLI", context_window=32768, max_output_tokens=4096)
    adapter = CLIAgentAdapter("codex_cli")
    provider = ProviderService(manifest=manifest, adapter=adapter, store=b, task_store=TaskOrderStore(tmp_path / "records"),
                               balance=TaskBalanceLedger(tmp_path / "balance.json"), messaging_key=bk)
    codex.replies.append(propose(path="a.txt"))
    key = codex_threads.window_key("task_alice")
    request = seal_task(body={"task_id": "order-1", "service_id": "codex-cli", "max_tokens": 64, "max_amount": 1,
        "chat": {"messages": conversation(), "tools": TOOLS, "max_tokens": 64},
        "conversation_key": key, "requester": "local", "conversation_scope": {"requester": "local"},
        "reply_messaging_pub": peer_box.public_key_b64(ak)}, task_id="order-1", kind="llm_request",
        sender_peer_id=a.peer_id, recipient_peer_id=b.peer_id, sender_signing_key=a.private_key_bytes,
        recipient_messaging_pub=peer_box.public_key_b64(bk), expires_at=_expires(300)).to_dict()
    from rynmesh.llm_package.task_protocol import open_task
    _, response = open_task(provider.handle(request), recipient_peer_id=a.peer_id,
                            recipient_messaging_key=ak, expected_kind="llm_response")
    assert response["state"] == "succeeded" and response["finish_reason"] == "tool_calls", response
    (kept,) = codex_threads.pool_for(adapter).idle
    assert kept.key[-2:] == ("peer:" + a.peer_id, key)


def test_local_api_key_is_the_requester_for_api_clients(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from rynmesh.llm_package.api import install_inference_api
    from rynmesh.llm_package.manifest import LLMPackageManifest

    seen = []

    class Adapter:
        supports_conversation_scope = True

        def health(self):
            return {"ok": True}

        def chat(self, body, **options):
            seen.append(options)
            return {"text": "ready", "message": {"role": "assistant", "content": "ready"},
                    "finish_reason": "stop", "input_tokens": 5, "output_tokens": 1, "duration_ms": 1}

    manager = SimpleNamespace(adapter=Adapter(), manifest=LLMPackageManifest(
        package_id="codex-cli", mode="codex_cli", adapter="codex_cli", public_model_alias="Codex CLI", runtime="external",
        capabilities=["text"], context_window=16000, max_output_tokens=512),
        _slots=threading.BoundedSemaphore(1), _lock=threading.Lock(), _running=0,
        context_window_for_model=lambda _="": 16000)
    app = FastAPI()
    install_inference_api(app, home=tmp_path, store=SimpleNamespace(peer_id="self"), active_manager=lambda: None,
                          local_manager_for=lambda service_id: manager if service_id == "codex-cli" else None,
                          discover=lambda _: [], execute_order=None, cancel_order=None)
    client = TestClient(app)
    for name in ("one", "two"):
        key = client.post("/api/local/llm/api-keys", json={"name": name}).json()["key"]
        response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key},
                               json={"model": "local/codex-cli", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 12})
        assert response.status_code == 200, response.text
    first, second = (options["conversation_scope"] for options in seen)
    assert first["requester"].startswith("local-api:") and first["window"] is None
    assert first["requester"] != second["requester"]
