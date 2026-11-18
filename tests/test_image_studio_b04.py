"""Persistent image assets, access scoping and queued-job references."""

import json

from test_image_studio import call, dims, headers, png, studio, submit
from test_public_plugins import setup as packaged_plugins
from ryn_image_studio.assets import ASSET_LIMIT, MAX_ASSET_BYTES
from ryn_image_studio.jobs import JobStore, RETENTION_S
from ryn_image_studio.site import StudioSite
from rynmesh.node_sites.client import SiteClient


IMAGE = png(64, 64)
OWNER = {
    "X-Ryn-Caller-Peer": "local-preview",
    "X-Ryn-Caller-Member": "owner",
    "X-Ryn-Caller-Name": "This node",
    "X-Ryn-Caller-Resource": "local",
    "X-Ryn-Site-Token": "",
}
OTHER = headers(member="colleague-other", peer="other-peer")


def create(site, kind="product", who=None, *, public=False, name="Test+asset", raw=IMAGE):
    path = f"/api/assets?name={name}&kind={kind}" + ("&public=1" if public else "")
    return call(site, "POST", path, raw, who)


def add(site, kind="product", who=None, **kwargs):
    status, _, data = create(site, kind, who, **kwargs)
    assert status == 200, data
    return json.loads(data)


def listing(site, who=None):
    status, _, data = call(site, "GET", "/api/assets", who=who)
    assert status == 200, data
    return json.loads(data)


def asset_path(asset, action=""):
    return f"/api/assets/{asset['id']}{action}"


def test_assets_are_private_persistent_and_deleted_with_their_thumbnail(tmp_path):
    store = JobStore(tmp_path, clock=lambda: 0)
    try:
        site = StudioSite(store)
        assert listing(site) == {"mine": [], "public": []}
        asset = add(site)
        assert asset["name"] == "Test asset"
        assert asset["kind"] == "product"
        assert call(site, "GET", asset_path(asset, "/thumb"), who=OTHER)[0] == 404
        assert call(site, "DELETE", asset_path(asset), who=OTHER)[0] == 404
        assert call(site, "DELETE", asset_path(asset), who=OWNER)[0] == 404
        assert listing(site, OTHER)["mine"] == []
        assert listing(site, headers(peer="another-device"))["mine"] == [asset]
    finally:
        store.close()

    store = JobStore(tmp_path, clock=lambda: RETENTION_S + 1)
    try:
        site = StudioSite(store)
        store.prune()
        assert listing(site) == {"mine": [asset], "public": []}
        status, response_headers, thumb = call(site, "GET", asset_path(asset, "/thumb"))
        assert status == 200 and response_headers["content-type"] == "image/jpeg"
        assert dims(thumb) == (64, 64)
        assert len(list(store.assets.root.iterdir())) == 2
        assert call(site, "DELETE", asset_path(asset))[0] == 200
        assert listing(site)["mine"] == []
        assert call(site, "GET", asset_path(asset, "/thumb"))[0] == 404
        assert list(store.assets.root.iterdir()) == []
    finally:
        store.close()


def test_public_asset_mutation_requires_local_owner_and_is_shared_with_colleagues(studio):
    store, worker, site, fake = studio
    assert json.loads(call(site, "GET", "/api/me", who=OWNER)[2])["is_owner"] is True
    assert json.loads(call(site, "GET", "/api/me")[2])["is_owner"] is False
    assert create(site, public=True)[0] == 403
    remote_owner = {**OWNER, "X-Ryn-Site-Token": "host-issued"}
    assert create(site, who=remote_owner, public=True)[0] == 403

    asset = add(site, "logo", OWNER, public=True)
    assert listing(site, OWNER) == listing(site, OTHER) == {"mine": [], "public": [asset]}
    assert call(site, "GET", asset_path(asset, "/thumb"), who=OTHER)[0] == 200
    assert call(site, "DELETE", asset_path(asset), who=OTHER)[0] == 403

    job = submit(site, who=OTHER, references=[{"asset": asset["id"]}])[0]
    worker.run_one(store.claim())
    assert job["reference_roles"] == ["material"]
    assert dims(fake.calls[-1]["references"][0]) == (64, 64)
    assert call(site, "DELETE", asset_path(asset), who=OWNER)[0] == 200
    assert listing(site, OTHER)["public"] == []
    assert call(site, "GET", f"/api/jobs/{job['id']}/image", who=OTHER)[0] == 200


def test_asset_count_and_size_limits_apply_per_owner(studio):
    _, _, site, _ = studio
    for _ in range(ASSET_LIMIT):
        add(site)
    status, _, data = create(site)
    assert (status, json.loads(data)["code"]) == (409, "image_asset_limit")
    add(site, who=OTHER)
    status, _, data = create(site, who=OTHER, raw=b"x" * (MAX_ASSET_BYTES + 1))
    assert (status, json.loads(data)["code"]) == (413, "image_upload_too_large")
    assert create(site, who=OTHER, raw=b"not a picture")[0] == 400
    assert create(site, kind="video", who=OTHER)[0] == 400
    assert create(site, who=OTHER, name="+")[0] == 400


def test_reference_roles_and_mixed_uploads_survive_prune(studio):
    store, worker, site, fake = studio
    product = add(site, "product", raw=png(64, 64, (80, 10, 10)))
    logo = add(site, "logo", raw=png(64, 64, (10, 80, 10)))
    style = add(site, "style", raw=png(64, 64, (10, 10, 80)))
    _, _, data = call(site, "POST", "/api/uploads", IMAGE)
    upload = json.loads(data)["upload_id"]
    job = submit(site, references=[
        {"asset": product["id"]}, {"asset": logo["id"]}, {"asset": style["id"]},
        {"asset": style["id"], "role": "material"}, upload,
    ])[0]
    assert job["reference_roles"] == ["material", "material", "style", "material", "material"]
    store.prune()
    worker.run_one(store.claim())
    expected = [
        png(64, 64, (80, 10, 10)), png(64, 64, (10, 80, 10)),
        png(64, 64, (10, 10, 80)), png(64, 64, (10, 10, 80)), IMAGE,
    ]
    assert fake.calls[-1]["references"] == expected

    request = json.dumps({"purpose": "main", "references": [{"asset": product["id"]}]}).encode()
    status, _, data = call(site, "POST", "/api/jobs", request, OTHER)
    assert (status, json.loads(data)["code"]) == (404, "image_reference_missing")

    request = json.dumps({"purpose": "main", "references": [{"asset": product["id"], "role": "source"}]}).encode()
    assert call(site, "POST", "/api/jobs", request)[0] == 400


def test_deleting_a_queued_reference_reports_it_missing(studio):
    store, worker, site, fake = studio
    asset = add(site)
    job = submit(site, references=[{"asset": asset["id"]}])[0]
    assert call(site, "DELETE", asset_path(asset))[0] == 200
    worker.run_one(store.claim())
    assert fake.calls == []
    status, _, data = call(site, "GET", f"/api/jobs/{job['id']}")
    assert status == 200
    failed = json.loads(data)
    assert failed["state"] == "failed"
    assert failed["failure"] == "image_reference_missing"
    assert failed["failure_text"] == "参考图不见了"
    assert failed["reference_roles"] == ["material"]


def test_local_plugin_open_provides_owner_identity(packaged_plugins):
    manager, sites, _, _, _ = packaged_plugins
    manager.refresh()
    manager.install("image-studio")
    client = SiteClient(sites.engine, sites.store)
    client.local_fetch = manager.local_fetch
    key = manager.open("image-studio")["url"].split("//")[1].split(".")[0]

    status, _, data = client.fetch(key, "GET", "/api/me", [], b"")
    assert status == 200 and json.loads(data)["is_owner"] is True
    status, _, data = client.fetch(key, "POST", "/api/assets?name=Local&kind=product&public=1", [], IMAGE)
    assert status == 200
    asset = json.loads(data)
    assert client.fetch(key, "GET", asset_path(asset, "/thumb"), [], b"")[0] == 200
    assert client.fetch(key, "DELETE", asset_path(asset), [], b"")[0] == 200
