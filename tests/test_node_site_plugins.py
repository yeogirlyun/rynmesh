"""Installed-site discovery, owner routing, failure isolation and path boundaries."""
import json
import sys
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rynmesh.node_sites.plugins import install_site_plugins


def package(home, name="sample-site", code=None):
    root = home / "site-plugins" / name
    root.mkdir(parents=True)
    module = name.replace("-", "_")
    (root / (module + ".py")).write_text(code or "def install(app, **kw):\n    return type('Plugin', (), {'owner_path': '/api/local/site-plugins/sample-site/owner/'})()\n")
    meta = root / (module + "-1.0.dist-info")
    meta.mkdir()
    (meta / "METADATA").write_text(f"Name: {name}\nVersion: 1.0\n")
    (meta / "entry_points.txt").write_text(f"[rynmesh.node_sites]\n{name} = {module}:install\n")
    return name


def setup():
    return FastAPI(), SimpleNamespace(owner_paths={}, host=SimpleNamespace(builtins={}))


def test_empty_node_has_no_application(tmp_path):
    app, sites = setup()
    assert install_site_plugins(app, home=tmp_path, sites=sites, local_control=lambda r: None) == {}
    assert sites.owner_paths == {}


def test_explicit_activation_and_failure_isolation(tmp_path):
    good = package(tmp_path)
    bad = package(tmp_path, "broken-site", "def install(app, **kw):\n    app.get('/partial')(lambda: {})\n    kw['sites'].host.builtins['broken-site'] = lambda: None\n    raise RuntimeError('broken')\n")
    root = tmp_path / "site-plugins"
    (root / "enabled.json").write_text(json.dumps({"plugins": {good: good, bad: bad, "escape": "../outside"}}))
    app, sites = setup()
    try:
        result = install_site_plugins(app, home=tmp_path, sites=sites, local_control=lambda r: None)
        assert result[good] == {"status": "ready", "version": "1.0"}
        assert result[bad]["status"] == result["escape"]["status"] == "failed"
        assert sites.owner_paths[good].endswith("/owner/")
        assert not sites.host.builtins
        assert TestClient(app).get('/partial').status_code == 404
    finally:
        for name in (good, bad):
            sys.modules.pop(name.replace('-', '_'), None)
            path = str(root / name)
            if path in sys.path: sys.path.remove(path)


def test_unactivated_package_does_not_execute(tmp_path):
    package(tmp_path, code="raise RuntimeError('must not import')")
    app, sites = setup()
    assert install_site_plugins(app, home=tmp_path, sites=sites, local_control=lambda r: None) == {}
