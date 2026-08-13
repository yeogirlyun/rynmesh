"""Rynmesh provider worker for Signal50 Veo rendering jobs."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .store import RynmeshStore

DEFAULT_CAPABILITY = "signal50.veo_motion.v1"
DEFAULT_OPERATION = "signal50.remote_action.complete_flow_video_veo_motion_clips"
RELAY_BUNDLE_OPERATION = "signal50.veo_motion.relay_bundle.v1"
DEFAULT_ACTION_ID = "complete_flow_video_veo_motion_clips"
DEFAULT_WORK_DIR = "~/.rynmesh/signal50-veo-worker"
DEFAULT_MEDIA_OPS_URL = "http://127.0.0.1:5055"
TERMINAL_RESULT_STATUSES = {"completed", "failed", "cancelled"}


class Signal50ServiceError(RuntimeError):
    pass


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    store = RynmeshStore()
    network_id = args.network_id or os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-home-qa")
    capability = args.capability or os.environ.get("RYNMESH_VEO_CAPABILITY", DEFAULT_CAPABILITY)
    api_url = (args.signal50_api_url or os.environ.get("SIGNAL50_API_URL") or "http://127.0.0.1:5050").rstrip("/")
    token = args.signal50_api_token or _load_signal50_token(args.signal50_repo)
    if not token:
        raise Signal50ServiceError("signal50_api_token_required")

    registration = store.register_job_capacity(
        capabilities=(capability,),
        network_id=network_id,
        capacity_units=1,
        max_concurrent=1,
        price_credits={capability: float(args.price_credits)},
        polling_interval_sec=int(args.poll_sec),
        metadata=_service_metadata(
            api_url=api_url,
            action_id=DEFAULT_ACTION_ID,
            relay_url=args.relay_url or _default_relay_url(),
            work_dir=args.work_dir,
            media_ops_url=args.media_ops_url,
        ),
    )
    print(json.dumps({"event": "capacity_registered", "result": registration}, sort_keys=True))

    while True:
        _service_once(
            store,
            network_id=network_id,
            capability=capability,
            api_url=api_url,
            token=token,
            job_timeout_sec=float(args.job_timeout_sec),
            work_dir=Path(args.work_dir).expanduser(),
            lock_file=Path(args.lock_file).expanduser(),
            relay_url=args.relay_url or _default_relay_url(),
            signal50_repo=args.signal50_repo,
            max_scenes_default=int(args.max_scenes_default or 0),
            media_ops_url=args.media_ops_url,
        )
        time.sleep(float(args.poll_sec))


def _service_once(
    store: RynmeshStore,
    *,
    network_id: str,
    capability: str,
    api_url: str,
    token: str,
    job_timeout_sec: float,
    work_dir: Path,
    lock_file: Path,
    relay_url: str,
    signal50_repo: str,
    max_scenes_default: int = 0,
    media_ops_url: str = DEFAULT_MEDIA_OPS_URL,
) -> None:
    orders = store.poll_work_orders(network_id=network_id, capability=capability).get("work_orders", [])
    for order in orders:
        work_order_id = str(order.get("work_order_id", ""))
        requester_peer_id = str(order.get("requester_peer_id", ""))
        if not work_order_id or not requester_peer_id:
            continue
        try:
            store.publish_work_result(
                work_order_id=work_order_id,
                requester_peer_id=requester_peer_id,
                status="accepted",
                message="Signal50 Veo provider accepted the work order.",
                network_id=network_id,
            )
            with _exclusive_worker_lock(lock_file):
                if _work_order_has_terminal_result(
                    store,
                    work_order_id=work_order_id,
                    network_id=network_id,
                ):
                    continue
                store.publish_work_result(
                    work_order_id=work_order_id,
                    requester_peer_id=requester_peer_id,
                    status="running",
                    message="Signal50 Veo provider is running this work order.",
                    network_id=network_id,
                )
                completed = _run_order(
                    store,
                    order=order,
                    api_url=api_url,
                    token=token,
                    timeout_sec=job_timeout_sec,
                    work_dir=work_dir,
                    relay_url=relay_url,
                    signal50_repo=signal50_repo,
                    max_scenes_default=max_scenes_default,
                    media_ops_url=media_ops_url,
                )
            status = str(completed.get("status", ""))
            if status == "completed":
                store.publish_work_result(
                    work_order_id=work_order_id,
                    requester_peer_id=requester_peer_id,
                    status="completed",
                    message=str(completed.get("message") or "Signal50 Veo job completed."),
                    result_content_ids=tuple(
                        str(item)
                        for item in completed.get("result_content_ids", [])
                        if str(item)
                    ),
                    result_refs=dict(completed.get("result_refs", {}) or {}),
                    credit_amount=float(order.get("max_credit_cost") or 0.0),
                    network_id=network_id,
                )
            else:
                store.publish_work_result(
                    work_order_id=work_order_id,
                    requester_peer_id=requester_peer_id,
                    status="failed",
                    message=str(completed.get("error") or completed.get("detail") or status),
                    result_content_ids=tuple(
                        str(item)
                        for item in completed.get("result_content_ids", [])
                        if str(item)
                    ),
                    result_refs=dict(completed.get("result_refs", {}) or {}),
                    network_id=network_id,
                )
        except Exception as exc:
            store.publish_work_result(
                work_order_id=work_order_id,
                requester_peer_id=requester_peer_id,
                status="failed",
                message=str(exc),
                network_id=network_id,
            )


def _run_order(
    store: RynmeshStore,
    *,
    order: dict[str, Any],
    api_url: str,
    token: str,
    timeout_sec: float,
    work_dir: Path,
    relay_url: str,
    signal50_repo: str,
    max_scenes_default: int = 0,
    media_ops_url: str = DEFAULT_MEDIA_OPS_URL,
) -> dict[str, Any]:
    del store, work_dir, api_url, token
    params = dict(order.get("params", {}) or {})
    if _relay_bundle_from_params(params) is not None:
        job = _submit_media_ops_job(
            media_ops_url,
            order=order,
            relay_url=relay_url,
            signal50_repo=signal50_repo,
            max_scenes_default=max_scenes_default,
        )
        completed = _wait_media_ops_job(
            media_ops_url,
            str(job["job_id"]),
            timeout_sec=timeout_sec,
        )
        status = str(completed.get("status", ""))
        result = dict(completed.get("result", {}) or {})
        if status != "completed" and not result:
            result = {
                "status": "failed",
                "detail": str(completed.get("error") or status),
                "result_refs": {"media_ops": completed},
            }
        return result
    raise Signal50ServiceError(
        "flow_job_bundle_required: Signal50 browser ops must be submitted "
        "as a relay bundle through the media-ops queue."
    )


def _submit_signal50_job(api_url: str, token: str, order_params: dict[str, Any]) -> dict[str, Any]:
    action_id = str(order_params.pop("action_id", "") or DEFAULT_ACTION_ID)
    title = str(order_params.pop("title", "") or "Rynmesh Signal50 Veo render")
    params = dict(order_params.pop("params", {}) or order_params)
    payload = _signal50_request(
        api_url,
        token,
        "POST",
        "/api/jobs",
        {
            "action_id": action_id,
            "params": params,
            "title": title,
            "requested_by": "rynmesh",
        },
    )
    job = payload.get("job", {})
    if not isinstance(job, dict) or not job.get("job_id"):
        raise Signal50ServiceError("signal50_job_response_invalid")
    return job


def _submit_media_ops_job(
    media_ops_url: str,
    *,
    order: dict[str, Any],
    relay_url: str,
    signal50_repo: str,
    max_scenes_default: int = 0,
) -> dict[str, Any]:
    params = dict(order.get("params", {}) or {})
    if max_scenes_default and not int(params.get("max_scenes") or 0):
        params["max_scenes"] = int(max_scenes_default)
    if relay_url and not str(params.get("relay_url") or "").strip():
        params["relay_url"] = relay_url
    if signal50_repo and not str(params.get("signal50_repo") or "").strip():
        params["signal50_repo"] = signal50_repo
    payload = _media_ops_request(
        media_ops_url,
        "POST",
        "/api/jobs",
        {
            "operation": RELAY_BUNDLE_OPERATION,
            "work_order_id": str(order.get("work_order_id") or ""),
            "params": params,
        },
    )
    job = payload.get("job", {})
    if not isinstance(job, dict) or not job.get("job_id"):
        raise Signal50ServiceError("media_ops_job_response_invalid")
    return job


def _wait_media_ops_job(
    media_ops_url: str,
    job_id: str,
    *,
    timeout_sec: float,
) -> dict[str, Any]:
    deadline = time.time() + max(1.0, timeout_sec)
    latest: dict[str, Any] = {"job_id": job_id, "status": "unknown"}
    while time.time() < deadline:
        payload = _media_ops_request(media_ops_url, "GET", f"/api/jobs/{job_id}")
        job = payload.get("job", {})
        if isinstance(job, dict):
            latest = job
            if str(job.get("status", "")) in TERMINAL_RESULT_STATUSES:
                return job
        time.sleep(float(os.environ.get("RYNMESH_MEDIA_OPS_JOB_POLL_SEC", "15") or 15))
    return {**latest, "status": "failed", "error": "media_ops_job_timeout"}


def _wait_signal50_job(api_url: str, token: str, job_id: str, *, timeout_sec: float) -> dict[str, Any]:
    deadline = time.time() + max(1.0, timeout_sec)
    latest: dict[str, Any] = {"job_id": job_id, "status": "unknown"}
    while time.time() < deadline:
        payload = _signal50_request(api_url, token, "GET", f"/api/jobs/{job_id}")
        job = payload.get("job", {})
        if isinstance(job, dict):
            latest = job
            if str(job.get("status", "")) in {"completed", "failed", "cancelled"}:
                return job
        time.sleep(float(os.environ.get("RYNMESH_SIGNAL50_JOB_POLL_SEC", "15") or 15))
    return {**latest, "status": "failed", "error": "signal50_job_timeout"}


def _media_ops_request(
    media_ops_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = str(media_ops_url or DEFAULT_MEDIA_OPS_URL).strip().rstrip("/")
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    request = Request(
        base + path,
        data=body,
        headers={"content-type": "application/json"},
        method=method,
    )
    try:
        timeout = float(os.environ.get("RYNMESH_MEDIA_OPS_TIMEOUT_SEC", "30") or 30)
        with urlopen(request, timeout=timeout) as response:
            data = response.read(10 * 1024 * 1024)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise Signal50ServiceError(f"media_ops_http_error: {exc}") from exc
    parsed = json.loads(data.decode("utf-8") or "{}")
    if not isinstance(parsed, dict):
        raise Signal50ServiceError("media_ops_response_not_object")
    if parsed.get("ok") is False:
        raise Signal50ServiceError(str(parsed))
    return parsed


@dataclass(frozen=True)
class RelayBundleRef:
    content_hash: str
    relay_url: str = ""
    filename: str = ""


def _run_relay_bundle_order(
    store: RynmeshStore,
    *,
    order: dict[str, Any],
    work_dir: Path,
    relay_url: str,
    signal50_repo: str,
    timeout_sec: float,
    max_scenes_default: int = 0,
) -> dict[str, Any]:
    params = dict(order.get("params", {}) or {})
    bundle = _relay_bundle_from_params(params)
    if bundle is None:
        raise Signal50ServiceError("flow_job_bundle_required")
    if not bundle.relay_url and not relay_url:
        raise Signal50ServiceError("relay_url_required")

    work_order_id = str(order.get("work_order_id", "") or "").strip()
    job_root = work_dir / work_order_id
    request_zip = job_root / "request_bundle.zip"
    extract_dir = job_root / "request"
    result_zip = job_root / "result_bundle.zip"
    job_root.mkdir(parents=True, exist_ok=True)
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    active_relay_url = bundle.relay_url or relay_url
    download = store.download_relay_artifact(
        bundle.content_hash,
        request_zip,
        relay_url=active_relay_url,
    )
    _safe_extract_zip(request_zip, extract_dir)
    flow_job_dir = _find_flow_job_dir(extract_dir)
    _relocate_flow_job_paths(flow_job_dir)

    max_scenes = int(
        params.get("max_scenes")
        or params.get("stop_after_clips")
        or max_scenes_default
        or 0
    )
    result_payload = _run_signal50_flow_bundle(
        flow_job_dir,
        signal50_repo=signal50_repo,
        timeout_sec=timeout_sec,
        max_scenes=max_scenes,
    )
    result_payload["relay_request_bundle"] = {
        "content_hash": bundle.content_hash,
        "download": download,
    }
    result_payload["work_order_id"] = work_order_id
    (job_root / "work_result.json").write_text(
        json.dumps(result_payload, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    _write_zip(
        result_zip,
        {
            "flow_job": flow_job_dir,
            "work_result.json": job_root / "work_result.json",
        },
    )
    uploaded = store.upload_relay_artifact(
        result_zip,
        relay_url=active_relay_url,
        media_type="application/zip",
        filename=(
            f"{_safe_filename(str(result_payload.get('video_id') or 'signal50'))}"
            f"-{work_order_id}-veo-result.zip"
        ),
    )
    result_content_hash = str(uploaded.get("blob", {}).get("content_hash", "") or "")
    status = "completed" if str(result_payload.get("status")) == "completed" else "failed"
    return {
        "status": status,
        "message": "Signal50 Veo relay bundle completed." if status == "completed" else "",
        "detail": str(result_payload.get("detail") or result_payload.get("status") or ""),
        "result_content_ids": [result_content_hash] if result_content_hash else [],
        "result_refs": {
            "signal50": result_payload,
            "relay_result_bundle": uploaded,
            "relay_mode": "rynmesh_http_relay",
        },
    }


def _run_signal50_flow_bundle(
    flow_job_dir: Path,
    *,
    signal50_repo: str,
    timeout_sec: float,
    max_scenes: int,
) -> dict[str, Any]:
    _ensure_signal50_import_path(signal50_repo)
    from signal50.flow_jobs import load_flow_job
    from signal50.integrations.hammerspoon_bridge import HammerspoonTarget
    from signal50.integrations.playwright_flow_adapter import (
        close_dedicated_flow_tabs,
        run_dedicated_cdp_flow_batch,
    )

    manifest = load_flow_job(flow_job_dir)
    target = HammerspoonTarget.from_env(browser_app="Google Chrome")
    result = None
    try:
        result = run_dedicated_cdp_flow_batch(
            job_dir=flow_job_dir,
            target=target,
            timeout_seconds=int(timeout_sec),
            max_scenes=max(0, int(max_scenes or 0)),
            scene_interleave_seconds=0,
        )
    finally:
        with contextlib.suppress(Exception):
            close_dedicated_flow_tabs(target=target)
    payload = result.to_payload() if result is not None else {}
    status = str(payload.get("status", "") or "").strip()
    (flow_job_dir / "flow_cdp_result.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return {
        "video_id": str(getattr(manifest, "video_id", "") or ""),
        "topic_title": str(getattr(manifest, "topic_title", "") or ""),
        "status": "completed" if status == "completed" else "failed",
        "detail": str(payload.get("detail") or status),
        "flow_job_dir": str(flow_job_dir),
        "flow_result": payload,
        "scene_outputs": list(payload.get("scene_outputs", []) or []),
        "completed_scene_count": int(payload.get("completed_scene_count", 0) or 0),
        "scene_count": int(payload.get("scene_count", 0) or 0),
    }


def _relay_bundle_from_params(params: dict[str, Any]) -> RelayBundleRef | None:
    raw = params.get("flow_job_bundle") or params.get("job_bundle") or {}
    if not isinstance(raw, dict):
        raw = {}
    content_hash = str(
        raw.get("content_hash")
        or raw.get("hash")
        or params.get("flow_job_bundle_hash")
        or params.get("bundle_content_hash")
        or ""
    ).strip()
    if not content_hash:
        return None
    return RelayBundleRef(
        content_hash=content_hash,
        relay_url=str(raw.get("relay_url") or params.get("relay_url") or "").strip(),
        filename=str(raw.get("filename") or "").strip(),
    )


def _safe_extract_zip(zip_path: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve()
            if target != destination and not str(target).startswith(str(destination) + os.sep):
                raise Signal50ServiceError(f"unsafe_zip_member: {member.filename}")
        archive.extractall(destination)


def _find_flow_job_dir(root: Path) -> Path:
    for candidate in (root / "flow_job", root / "job", root):
        if (candidate / "storyboard.json").exists():
            return candidate
    matches = sorted(root.glob("**/storyboard.json"))
    if not matches:
        raise Signal50ServiceError("flow_storyboard_not_found")
    return matches[0].parent


def _relocate_flow_job_paths(job_dir: Path) -> None:
    storyboard = job_dir / "storyboard.json"
    payload = json.loads(storyboard.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise Signal50ServiceError("flow_storyboard_not_object")
    prompts_dir = job_dir / "prompts"
    downloads_dir = job_dir / "downloads"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    downloads_dir.mkdir(parents=True, exist_ok=True)
    payload["storyboard_path"] = str(storyboard)
    payload["scene_prompts_dir"] = str(prompts_dir)
    payload["downloads_dir"] = str(downloads_dir)
    master_prompt_path = payload.get("master_prompt_path")
    if master_prompt_path:
        payload["master_prompt_path"] = str(job_dir / Path(str(master_prompt_path)).name)
    scenes = payload.get("scenes", [])
    if not isinstance(scenes, list):
        raise Signal50ServiceError("flow_scenes_not_list")
    for index, scene in enumerate(scenes, start=1):
        if not isinstance(scene, dict):
            continue
        scene_id = str(scene.get("scene_id") or f"scene_{index:02d}").strip()
        scene["scene_id"] = scene_id
        scene_dir = downloads_dir / scene_id
        scene_dir.mkdir(parents=True, exist_ok=True)
        expected_name = Path(str(scene.get("expected_asset") or f"{scene_id}.mp4")).name
        scene["expected_asset"] = str(scene_dir / expected_name)
        prompt_name = Path(str(scene.get("prompt_path") or f"{scene_id}.txt")).name
        scene["prompt_path"] = str(prompts_dir / prompt_name)
        for key in (
            "confirmed_asset",
            "reference_asset",
            "kling_start_keyframe_path",
            "kling_end_keyframe_path",
        ):
            value = str(scene.get(key) or "").strip()
            if not value:
                continue
            relocated = scene_dir / Path(value).name
            scene[key] = str(relocated) if relocated.exists() else ""
    storyboard.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _write_zip(zip_path: Path, entries: dict[str, Path]) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for arc_root, source in entries.items():
            if source.is_dir():
                for path in sorted(source.rglob("*")):
                    if path.is_file():
                        archive.write(path, Path(arc_root) / path.relative_to(source))
            elif source.is_file():
                archive.write(source, arc_root)


def _ensure_signal50_import_path(signal50_repo: str = "") -> None:
    import importlib
    import importlib.util

    candidates = [
        Path(signal50_repo).expanduser() if signal50_repo else Path(),
        Path(os.environ.get("SIGNAL50_REPO", "")).expanduser(),
        Path("~/signal50").expanduser(),
    ]
    avaryn_candidates: list[Path] = []
    configured_avaryn = str(os.environ.get("AVARYN_REPO", "") or "").strip()
    if configured_avaryn:
        avaryn_candidates.append(Path(configured_avaryn).expanduser())
    avaryn_candidates.extend([
        Path("~/avaryn").expanduser(),
    ])
    for repo in avaryn_candidates:
        if not str(repo) or not (repo / "avaryn").exists():
            continue
        root = str(repo.resolve())
        if root not in sys.path:
            sys.path.insert(0, root)
    importlib.invalidate_caches()
    if importlib.util.find_spec("avaryn.lens") is None:
        sys.modules.pop("avaryn", None)
        importlib.invalidate_caches()
    for repo in candidates:
        if not str(repo) or not (repo / "src" / "signal50").exists():
            continue
        src = str((repo / "src").resolve())
        if src not in sys.path:
            sys.path.insert(0, src)
        return
    raise Signal50ServiceError("signal50_repo_not_found")


def _work_order_has_terminal_result(
    store: RynmeshStore,
    *,
    work_order_id: str,
    network_id: str,
) -> bool:
    results = store.list_work_results(work_order_id=work_order_id, network_id=network_id)
    for item in results.get("work_results", []):
        if str(item.get("status", "")).strip() in TERMINAL_RESULT_STATUSES:
            return True
    return False


@contextlib.contextmanager
def _exclusive_worker_lock(lock_file: Path):
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a+")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:
            pass
        handle.close()


def _safe_filename(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value)
    return cleaned.strip("-") or "signal50"


def _signal50_request(
    api_url: str,
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    request = Request(
        api_url.rstrip("/") + path,
        data=body,
        headers={
            "content-type": "application/json",
            "X-Signal50-Api-Token": token,
        },
        method=method,
    )
    try:
        timeout = float(os.environ.get("RYNMESH_SIGNAL50_API_TIMEOUT_SEC", "30") or 30)
        with urlopen(request, timeout=timeout) as response:
            data = response.read(10 * 1024 * 1024)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise Signal50ServiceError(f"signal50_http_error: {exc}") from exc
    parsed = json.loads(data.decode("utf-8") or "{}")
    if not isinstance(parsed, dict):
        raise Signal50ServiceError("signal50_response_not_object")
    if parsed.get("ok") is False:
        raise Signal50ServiceError(str(parsed))
    return parsed


def _service_metadata(
    *,
    api_url: str,
    action_id: str,
    relay_url: str = "",
    work_dir: str = "",
    media_ops_url: str = DEFAULT_MEDIA_OPS_URL,
) -> dict[str, Any]:
    return {
        "service": "Signal50 Veo rendering",
        "route": "m4_mini_hammerspoon_chrome_cdp",
        "operation": DEFAULT_OPERATION,
        "relay_operation": RELAY_BUNDLE_OPERATION,
        "signal50_api_url": api_url,
        "action_id": action_id,
        "max_concurrent": 1,
        "queue": "serialized_m4_hammerspoon_chrome",
        "media_ops_url": media_ops_url,
        "relay": {
            "required_for_artifacts": True,
            "url": relay_url,
            "input_bundle_param": "flow_job_bundle.content_hash",
            "result_bundle_ref": "result_refs.relay_result_bundle.blob.content_hash",
        },
        "work_dir": work_dir,
        "request_form": {
            "fields": [
                {"name": "video_id", "label": "Signal50 video ID", "required": True},
                {"name": "flow_job_bundle.content_hash", "label": "Relay Flow job bundle hash", "required": False},
                {"name": "skip_existing", "label": "Skip existing clips", "type": "boolean", "default": True},
                {"name": "max_scenes", "label": "Max scenes", "type": "integer", "default": 0},
            ]
        },
    }


def _load_signal50_token(signal50_repo: str = "") -> str:
    env_token = os.environ.get("SIGNAL50_API_TOKEN", "").strip()
    if env_token:
        return env_token
    candidates = []
    if signal50_repo:
        candidates.append(Path(signal50_repo).expanduser() / "config" / "api.local.yaml")
    candidates.extend(
        [
            Path("~/signal50/config/api.local.yaml").expanduser(),
        ]
    )
    for path in candidates:
        if not path.exists():
            continue
        token = _token_from_yaml_text(path.read_text(encoding="utf-8"))
        if token:
            return token
    return ""


def _default_relay_url() -> str:
    return os.environ.get("RYNMESH_RELAY_URL", "").strip() or os.environ.get(
        "RYNMESH_REGISTRY_URL",
        "",
    ).strip()


def _token_from_yaml_text(text: str) -> str:
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("token:"):
            continue
        value = line.split(":", 1)[1].strip()
        return value.strip("\"'")
    return ""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Rynmesh Signal50 Veo provider worker.")
    parser.add_argument("--network-id", default="")
    parser.add_argument("--capability", default="")
    parser.add_argument("--signal50-api-url", default="")
    parser.add_argument("--signal50-api-token", default="")
    parser.add_argument("--signal50-repo", default="")
    parser.add_argument("--relay-url", default=os.environ.get("RYNMESH_RELAY_URL", ""))
    parser.add_argument(
        "--media-ops-url",
        default=os.environ.get("SIGNAL50_MEDIA_OPS_URL", DEFAULT_MEDIA_OPS_URL),
    )
    parser.add_argument("--work-dir", default=os.environ.get("RYNMESH_SIGNAL50_VEO_WORK_DIR", DEFAULT_WORK_DIR))
    parser.add_argument(
        "--lock-file",
        default=os.environ.get(
            "RYNMESH_SIGNAL50_VEO_LOCK_FILE",
            str(Path(DEFAULT_WORK_DIR).expanduser() / "provider.lock"),
        ),
    )
    parser.add_argument(
        "--max-scenes-default",
        type=int,
        default=int(os.environ.get("RYNMESH_SIGNAL50_VEO_MAX_SCENES_DEFAULT", "0") or 0),
    )
    parser.add_argument(
        "--poll-sec",
        type=float,
        default=float(os.environ.get("RYNMESH_VEO_WORKER_POLL_SEC", "30") or 30),
    )
    parser.add_argument(
        "--job-timeout-sec",
        type=float,
        default=float(os.environ.get("RYNMESH_VEO_WORKER_JOB_TIMEOUT_SEC", "14400") or 14400),
    )
    parser.add_argument(
        "--price-credits",
        type=float,
        default=float(os.environ.get("RYNMESH_VEO_PRICE_CREDITS", "20") or 20),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
