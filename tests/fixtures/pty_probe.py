"""Local-only terminal acceptance command shared with exec_command/write_stdin."""
import json
import os
import subprocess
import sys
import time


def report(event, **values):
    print(json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


scenario = sys.argv[1]
report("terminal", stdin=sys.stdin.isatty(), stdout=sys.stdout.isatty(), pid=os.getpid())
if scenario == "interaction":
    report("prompt")
    answer = input("Name: ")
    report("answer", value=answer)
    report("size", columns=os.get_terminal_size().columns, lines=os.get_terminal_size().lines)
    for i in range(3):
        report("tick", number=i)
        time.sleep(0.2)
    try:
        report("interrupt_ready")
        while True:
            time.sleep(0.05)
    except KeyboardInterrupt:
        report("interrupted")
        # Ctrl+C must allow a cooperative program to remain alive.
        report("after_interrupt", value=input("Continue: "))
elif scenario in {"tree", "orphan"}:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    report("child", pid=child.pid)
    if scenario == "tree":
        time.sleep(120)
elif scenario == "eof":
    try:
        input("EOF: ")
    except EOFError:
        report("eof")
    report("after_eof", value=input("Still open: "))
elif scenario == "exit":
    report("last", value="你好🙂" * 100)
    sys.exit(7)
