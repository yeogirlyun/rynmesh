from rynmesh.services import net_egress_client as nec

SESSION = {"exit_host": "sz-egress-exit", "exit_user": "ops", "socks_port": 2080}

def test_connect_dry_run_plan_is_unchanged_by_extra_env():
    # extra_env must not change the argv plan, only the process env.
    argv = nec.connect(SESSION, mode="chrome", url="https://x", dry_run=True,
                        extra_env={"RYNMESH_VPN_PROFILE_DIR": "/tmp/sz"})
    assert argv == ["rynmesh-vpn", "chrome", "https://x"]

def test_connect_merges_extra_env_into_subprocess(monkeypatch):
    captured = {}
    def fake_run(argv, env=None, check=False):
        captured["env"] = env
        captured["argv"] = argv
        class R: returncode = 0
        return R()
    monkeypatch.setattr(nec.subprocess, "run", fake_run)
    monkeypatch.setattr(nec, "_vpn_cmd", lambda: "rynmesh-vpn")
    nec.connect(SESSION, mode="up", extra_env={"RYNMESH_VPN_PROFILE_DIR": "/tmp/sz"})
    assert captured["argv"] == ["rynmesh-vpn", "up"]
    assert captured["env"]["RYNMESH_VPN_PROFILE_DIR"] == "/tmp/sz"
    assert captured["env"]["RYNMESH_VPN_GATEWAY"] == "ops@sz-egress-exit"
    assert captured["env"]["RYNMESH_VPN_PORT"] == "2080"
