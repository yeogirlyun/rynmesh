"""PyInstaller entry: the stock Ryn node daemon, frozen self-contained.

This is the unmodified rynmesh peer (`rynmesh.peer_http:main`); freezing only
removes the system-Python/rynmesh install requirement. Behavior is identical.
"""
import multiprocessing
import sys


def _run_computer_use_package_check(
    argv=None,
    *,
    client_factory=None,
    is_frozen=None,
):
    """Execute the bounded R2 computer-use package check and return `(payload_dict, exit_code)`.

    Never catches `SystemExit` or writes to stdout directly; cleanup always completes in `finally`
    before the caller emits a single JSON line and exits once.
    """
    eff_argv = list(sys.argv if argv is None else argv)
    if len(eff_argv) != 2 or eff_argv[1] != "--ryn-computer-use-package-check":
        return (
            {
                "ok": False,
                "check": "ryn_computer_use_package_check",
                "errorCode": "package_check_unexpected_arguments",
            },
            2,
        )

    frozen_flag = (
        bool(getattr(sys, "frozen", False))
        if is_frozen is None
        else bool(is_frozen)
    )
    if not frozen_flag:
        return (
            {
                "ok": False,
                "check": "ryn_computer_use_package_check",
                "errorCode": "package_check_frozen_required",
            },
            1,
        )

    from rynmesh.agent_runtime.computer import (
        DEFAULT_CONTROL_TIMEOUT_S,
        NativeBridgeError,
        NativeWorkerClient,
        validate_shutdown_result,
        verify_lease_release_authority,
    )

    client = NativeWorkerClient() if client_factory is None else client_factory()
    session = None
    lease = None
    payload = None
    exit_code = 1
    try:
        session = client.ensure_session()
        lease = getattr(session, "_artifact_lease", None)
        proc_ident = getattr(session, "process_identity", None)
        handshake = getattr(session, "handshake", None)
        r1_proof_valid = bool(
            lease is not None
            and verify_lease_release_authority(
                lease,
                process_identity=proc_ident,
                handshake=handshake,
            )
        )
        snap = client.cache_status()
        binding = snap.get("binding") if isinstance(snap, dict) else None
        artifact_info = (
            binding.get("artifact") if isinstance(binding, dict) else None
        )
        process_info = (
            binding.get("process") if isinstance(binding, dict) else None
        )
        worker_info = snap.get("worker") if isinstance(snap, dict) else None

        raw_shutdown, _ = session.request(
            "shutdown",
            {},
            timeout=DEFAULT_CONTROL_TIMEOUT_S,
            phase="shutdown",
            pre_dispatch_allowed=True,
            pre_dispatch_on_send_error=True,
            is_control=True,
        )
        validated_shutdown = validate_shutdown_result(
            raw_shutdown,
            phase="shutdown",
        )
        shutdown_clean = bool(
            isinstance(validated_shutdown, dict)
            and validated_shutdown.get("shuttingDown") is True
        )
        client.close()
        if lease is not None:
            lease.close()

        proc_obj = getattr(session, "process", None)
        worker_terminated = bool(
            proc_obj is not None and proc_obj.poll() is not None
        )
        lease_closed = bool(lease is not None and lease.closed)
        after_snap = client.cache_status()
        client_closed = bool(
            isinstance(after_snap, dict)
            and after_snap.get("closed") is True
            and after_snap.get("live") is False
            and after_snap.get("verified") is False
            and after_snap.get("releaseVerified") is False
        )

        release_verified = bool(
            r1_proof_valid
            and isinstance(snap, dict)
            and snap.get("status") == "ready"
            and snap.get("mode") == "production"
            and snap.get("verified") is True
            and snap.get("releaseVerified") is True
            and isinstance(binding, dict)
            and binding.get("releaseVerified") is True
            and isinstance(artifact_info, dict)
            and isinstance(process_info, dict)
            and process_info.get("verifiedBy") == "win32_process_handle"
            and isinstance(worker_info, dict)
            and worker_info.get("pid") == process_info.get("pid")
        )
        all_ok = bool(
            release_verified
            and shutdown_clean
            and lease_closed
            and client_closed
            and worker_terminated
        )
        payload = {
            "ok": all_ok,
            "check": "ryn_computer_use_package_check",
            "mode": snap.get("mode") if isinstance(snap, dict) else None,
            "verified": bool(
                isinstance(snap, dict) and snap.get("verified") is True
            ),
            "releaseVerified": release_verified,
            "releaseId": (
                binding.get("releaseId") if isinstance(binding, dict) else None
            ),
            "manifestDigest": (
                binding.get("manifestDigest")
                if isinstance(binding, dict)
                else None
            ),
            "nativeSha256": (
                artifact_info.get("sha256")
                if isinstance(artifact_info, dict)
                else None
            ),
            "nativeSize": (
                artifact_info.get("size")
                if isinstance(artifact_info, dict)
                else None
            ),
            "targetTriple": (
                artifact_info.get("targetTriple")
                if isinstance(artifact_info, dict)
                else None
            ),
            "arch": (
                {
                    "machine": artifact_info.get("machine"),
                    "optionalHeaderMagic": artifact_info.get(
                        "optionalHeaderMagic"
                    ),
                }
                if isinstance(artifact_info, dict)
                else None
            ),
            "protocolVersion": (
                snap.get("protocolVersion") if isinstance(snap, dict) else None
            ),
            "worker": {
                "name": (
                    worker_info.get("name")
                    if isinstance(worker_info, dict)
                    else None
                ),
                "version": (
                    worker_info.get("version")
                    if isinstance(worker_info, dict)
                    else None
                ),
                "pid": (
                    process_info.get("pid")
                    if isinstance(process_info, dict)
                    else None
                ),
                "creationTime": (
                    process_info.get("creationTime")
                    if isinstance(process_info, dict)
                    else None
                ),
                "fileId": (
                    process_info.get("fileId")
                    if isinstance(process_info, dict)
                    else None
                ),
                "verifiedBy": (
                    process_info.get("verifiedBy")
                    if isinstance(process_info, dict)
                    else None
                ),
            },
            "cleanup": {
                "shutdownClean": shutdown_clean,
                "leaseClosed": lease_closed,
                "clientClosed": client_closed,
                "workerTerminated": worker_terminated,
            },
        }
        exit_code = 0 if all_ok else 1
    except NativeBridgeError as exc:
        payload = {
            "ok": False,
            "check": "ryn_computer_use_package_check",
            "errorCode": exc.code,
            "phase": exc.phase,
            "reason": exc.reason,
        }
        exit_code = 1
    except Exception:
        payload = {
            "ok": False,
            "check": "ryn_computer_use_package_check",
            "errorCode": "package_check_internal_error",
        }
        exit_code = 1
    finally:
        try:
            client.close()
        except Exception:
            pass
        if lease is not None:
            try:
                lease.close()
            except Exception:
                pass
    return payload, exit_code


def _emit_and_exit_computer_use_package_check(
    argv=None,
    *,
    stream=None,
    exit_fn=None,
    client_factory=None,
    is_frozen=None,
):
    """Run `_run_computer_use_package_check`, write exactly one JSON line to `stream`, flush, and exit once."""
    import json

    out = sys.stdout if stream is None else stream
    do_exit = sys.exit if exit_fn is None else exit_fn
    payload, exit_code = _run_computer_use_package_check(
        argv,
        client_factory=client_factory,
        is_frozen=is_frozen,
    )
    out.write(json.dumps(payload) + "\n")
    out.flush()
    do_exit(exit_code)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    if len(sys.argv) > 1 and sys.argv[1] == "--ryn-document-worker":
        from rynmesh.services.document_extract_child import main as document_main
        sys.exit(document_main())
    if len(sys.argv) > 1 and sys.argv[1] == "--ryn-native-worker":
        from rynmesh.llm_package.native_worker import main as native_main
        native_main(sys.argv[2:])
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--ryn-computer-use-worker":
        from rynmesh.agent_runtime.extensions.computer_use_server import main as computer_use_main
        computer_use_main()
        sys.exit(0)
    if len(sys.argv) > 1 and sys.argv[1] == "--ryn-computer-use-package-check":
        _emit_and_exit_computer_use_package_check(sys.argv)
    if len(sys.argv) > 1 and sys.argv[1] == "--ryn-private-cli-worker":
        from rynmesh.llm_package.process_guard import limited_exec
        limited_exec(sys.argv[2:])
        sys.exit(1)
    if len(sys.argv) > 1 and sys.argv[1] == "--restore-space":
        sys.argv.pop(1)
        from rynmesh.personal_space import main as restore_main
        restore_main()
        sys.exit(0)
    from rynmesh.peer_http import main
    sys.exit(main())
