"""Self-contained Windows launcher for the production node and browser UI.

The installer freezes this entry and registers rynmesh://. No system Python,
terminal, API key or model is required to consume a friend's AI.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from .atomic_io import atomic_write_json, read_json
from .friends.continuation import InvitationContinuation


def local_address() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(('192.0.2.1', 80))  # Routing lookup only; no packet is sent.
            return probe.getsockname()[0]
        except OSError:
            return socket.gethostbyname(socket.gethostname())


def main():
    parser = argparse.ArgumentParser(description='Ryn for Windows')
    parser.add_argument('invitation', nargs='?', default='')
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('RYNMESH_DESKTOP_HOME') or Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'Ryn' / 'data'))
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--loopback-acceptance', action='store_true')
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--headless', action='store_true')
    args = parser.parse_args()
    home = args.home.resolve()
    home.mkdir(parents=True, exist_ok=True)
    continuation = InvitationContinuation(home, allow_loopback=args.loopback_acceptance)
    if args.invitation:
        try:
            continuation.save(args.invitation)
        except (ValueError, OSError):
            from tkinter import messagebox
            messagebox.showerror('Ryn invitation', 'This invitation is invalid or could not be saved. Open Ryn → Friends and paste the complete invitation again.')
            return 1

    # Nonblocking process lock. A second protocol activation saves continuation
    # for the same home, then opens that running node rather than spawning one.
    import msvcrt
    lock = (home / '.desktop.lock').open('a+b')
    if lock.seek(0, 2) == 0:
        lock.write(b'0')
        lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        for _ in range(80):
            try:
                info = read_json(home / 'desktop-running.json')
                url = f"http://127.0.0.1:{int(info['port'])}"
                with urllib.request.urlopen(url + '/api/local/desktop/status', timeout=1) as response:
                    actual = json.load(response)
                if actual.get('instance') == info.get('instance'):
                    webbrowser.open(url + '/friends')
                    return 0
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.1)
        from tkinter import messagebox
        messagebox.showerror('Ryn is starting', 'Your invitation is saved. Open Ryn again when startup finishes.')
        return 1

    settings_path = home / 'desktop-settings.json'
    settings = read_json(settings_path) if settings_path.exists() else {}
    preferred = args.port or int(settings.get('port', 19600))
    bound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    bound.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    # Retain the bound socket through server startup; no free-port race.
    for port in range(preferred, preferred + (1 if args.port else 30)):
        try:
            bound.bind(('127.0.0.1' if args.loopback_acceptance else '0.0.0.0', port))
            break
        except OSError:
            if port == preferred + (0 if args.port else 29):
                raise RuntimeError('No Ryn port is available. Close another Ryn instance and try again.') from None
    bound.listen(128)
    atomic_write_json(settings_path, {'port': port})
    address = '127.0.0.1' if args.loopback_acceptance else local_address()
    os.environ.update(RYNMESH_HOME=str(home), RYNMESH_LLM_HOME=str(home / 'llm'),
        RYNMESH_FRIEND_ENDPOINT=f'http://{address}:{port}',
        RYNMESH_FRIEND_ALLOW_LOOPBACK='1' if args.loopback_acceptance else '0',
        RYNMESH_AUTO_REGISTER='0', RYNMESH_DISABLE_DISCOVERY='1', RYNMESH_REGISTRY_URL='',
        RYNMESH_REGISTRY_DIR=str(home / 'network/registry'), RYNMESH_RELAY_URL='', RYNMESH_LLM_RELAY_URL='')

    import uvicorn

    from .peer_http import create_app
    from .store import RynmeshStore
    app = create_app(RynmeshStore(home=home, network_dir=home / 'network', node_name=socket.gethostname()))
    instance = secrets.token_hex(16)
    # Install before the catch-all UI mount.
    router = APIRouter()
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, lifespan='on'))

    @router.get('/api/local/desktop/status')
    def desktop_status(request: Request):
        app.state.friends.local_control(request)
        return {'instance': instance, 'port': port}

    @router.post('/api/local/desktop/stop')
    def stop_desktop(request: Request):
        app.state.friends.local_control(request)
        if not server.started:
            raise HTTPException(409, detail='desktop_starting')
        server.should_exit = True
        return {'stopping': True}

    app.router.routes[0:0] = router.routes
    thread = threading.Thread(target=lambda: server.run(sockets=[bound]), daemon=True)
    thread.start()
    for _ in range(300):
        if server.started:
            break
        if not thread.is_alive():
            raise RuntimeError('Ryn could not start. Please reopen the application.')
        time.sleep(0.1)
    if not server.started:
        raise RuntimeError('Ryn is taking too long to start. Please reopen the application.')
    atomic_write_json(home / 'desktop-running.json', {'port': port, 'instance': instance, 'pid': os.getpid()})
    url = f'http://127.0.0.1:{port}'
    if not args.no_open:
        webbrowser.open(url + '/friends')
    try:
        if args.headless:
            thread.join()
        else:
            import tkinter as tk
            from tkinter import ttk
            window = tk.Tk()
            window.title('Ryn — your friend’s AI')
            window.geometry('540x280')
            window.minsize(500, 250)
            frame = ttk.Frame(window, padding=24)
            frame.pack(fill='both', expand=True)
            ttk.Label(frame, text='Ryn is ready', font=('Segoe UI', 20)).pack(anchor='w')
            ttk.Label(frame, text='Open Friends to review an invitation and start chatting.\nYou do not need a model or an API key.\nKeep Ryn running while using or sharing AI.', wraplength=470).pack(anchor='w', pady=16)
            ttk.Button(frame, text='Open Ryn / Continue invitation', command=lambda: webbrowser.open(url + '/friends')).pack(fill='x', pady=6)
            ttk.Button(frame, text='Stop Ryn', command=window.destroy).pack(fill='x', pady=6)
            def observe():
                if not thread.is_alive():
                    window.destroy()
                else:
                    window.after(500, observe)
            window.after(500, observe)
            window.mainloop()
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        bound.close()
        (home / 'desktop-running.json').unlink(missing_ok=True)
        lock.close()
    return 0
