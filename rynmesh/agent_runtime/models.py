"""Credential-free model pool, independent of local Agent preferences."""
from __future__ import annotations

import copy
import hashlib
import os
import threading
import time

from ..llm_package import model_settings, peer_binding
from ..llm_package.model_usage import ModelUsageStore
from ..llm_package.reasoning import metadata as reasoning_metadata
from ..llm_package.tool_capabilities import client_tools_contract
from .image_capabilities import catalog_image_fields
from . import diagnostic_log

_SPACE_CATALOG_ERROR_REASONS = {
    "space_catalog_unreachable": "无法连接设备（space_catalog_unreachable）",
    "space_catalog_rate_limited": "请求过于频繁（space_catalog_rate_limited）",
    "space_catalog_clock_skew": "两台设备系统时间相差过大，请校准时钟（space_catalog_clock_skew）",
    "space_catalog_peer_not_allowed": "个人空间访问权限未放行（space_catalog_peer_not_allowed）",
    "space_catalog_response_limit": "对端目录响应超出大小限制（space_catalog_response_limit）",
    "space_catalog_response_invalid": "对端目录响应校验未通过（space_catalog_response_invalid）",
    "space_catalog_capacity_exhausted": "目录刷新并发已满（space_catalog_capacity_exhausted）",
    "space_catalog_request_rejected": "对端拒绝目录请求（space_catalog_request_rejected）",
    "space_catalog_request_invalid": "目录请求格式无效（space_catalog_request_invalid）",
    "space_catalog_refresh_superseded": "目录刷新已被新请求取代（space_catalog_refresh_superseded）",
    "space_catalog_timeout": "等待目录刷新超时（space_catalog_timeout）",
}


def _format_catalog_error(code: str) -> str:
    return _SPACE_CATALOG_ERROR_REASONS.get(code, code)


def _cli_output(row, upstream):
    value = upstream.get("max_output_tokens")
    basis = "provider_cli_model_metadata"
    known = True
    if type(value) is not int:
        vendor = model_settings.known_vendor_output_metadata(upstream.get("id", ""))
        value = vendor["max_output_tokens"] if vendor else None
        basis = vendor["max_output_basis"] if vendor else "unknown_fallback"
        known = bool(vendor and vendor["max_output_tokens_known"])
    if type(value) is int and 0 < value <= row["context_window"]:
        row.update(max_output_tokens=value, max_output_tokens_known=known, max_output_basis=basis)
    else:
        row.update(max_output_tokens=4096, max_output_tokens_known=False, max_output_basis="unknown_fallback")


def _model_facts(row, upstream):
    from .model_profiles import CAPACITY_FACTS, FACTS, _valid_fact
    # Capacity values retain their existing known/basis handling above.
    for name in FACTS.keys() - CAPACITY_FACTS:
        if name in upstream and _valid_fact(name, upstream[name]):
            row[name] = upstream[name]


class ModelCatalog:
    def __init__(self, services, store):
        self.services, self.store = services, store
        self.network = os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main")
        # Only CLI model metadata is retained. Availability and data authority
        # are rebuilt from the current provider and exact model binding.
        self._cli_metadata = {}
        self._cli_metadata_lock = threading.Lock()
        self._learned_output_limits = {}
        self._learned_context_limits = {}
        self._catalog_cache = {}
        self._catalog_lock = threading.RLock()
        self._diagnostics = {}
        self._usage_store = getattr(self.services.get("source_store"), "usage_store", None)
        if self._usage_store is None:
            from pathlib import Path
            store_home = getattr(self.store, "home", None)
            usage_path = (store_home / "llm" / "model-usage.json" if store_home is not None
                          else getattr(self.services.get("source_store"), "path", Path(".")).parent / "model-usage.json")
            self._usage_store = ModelUsageStore(usage_path)

    def catalog_diagnostics(self):
        with self._catalog_lock:
            return copy.deepcopy(self._diagnostics)

    def learn_output_limit(self, target, cap):
        with self._cli_metadata_lock:
            self._learned_output_limits[(target["id"], target.get("destination_revision", ""))] = cap
        with self._catalog_lock:
            self._catalog_cache.clear()

    def learn_context_limit(self, target, cap):
        with self._cli_metadata_lock:
            self._learned_context_limits[(target["id"], target.get("destination_revision", ""))] = cap
        with self._catalog_lock:
            self._catalog_cache.clear()

    def mark_streaming_unknown(self, target):
        sources = self.services["source_store"]
        with sources.lock:
            record = sources.read().get(target["source_id"])
            if not record or self.source_destination_revision(record, target["model_id"]) != target.get("destination_revision"):
                return
            record.setdefault("streaming_support", {})[target["model_id"]] = "unknown"
            record.setdefault("streaming_checked_at", {})[target["model_id"]] = time.time()
            sources.save(record)
        with self._catalog_lock:
            self._catalog_cache.clear()

    @staticmethod
    def destination(row):
        return {"catalog_model_id": row["id"], "provider_peer_id": row.get("peer_id", ""),
                "source_id": row.get("source_id", ""), "destination_revision": row.get("destination_revision", "")}

    def source_destination_revision(self, record, model=None):
        if model and record.get("model_settings", {}).get(model, {}).get("destination_revision"):
            return record["model_settings"][model]["destination_revision"]
        if record.get("destination_revision"):
            return record["destination_revision"]
        # Legacy records migrate on their next save. Hash only encrypted bytes,
        # never a public digest of low-entropy credential material.
        return "legacy-" + hashlib.sha256(self.services["source_store"].path.read_bytes()).hexdigest()

    @staticmethod
    def _bind_cli(row, manager, model="", *, cached=False):
        binding = None
        try:
            method = "cached_binding_for_model" if cached else "binding_for_model"
            if manager and hasattr(manager.adapter, method):
                binding = getattr(manager.adapter, method)(model)
        except (ValueError, OSError, RuntimeError):
            pass  # No executable/configuration evidence means no project-data authority.
        valid = peer_binding.valid(binding, row["service_id"])
        row.update(local_model_binding=binding if valid else None,
                   destination_binding_supported=bool(valid),
                   destination_revision=peer_binding.local_revision(binding, row["peer_id"]) if valid else "")

    def list_models(self, *, selected_model_id=None, display_only=False):
        with self._catalog_lock:
            sources_revision = getattr(self.services["source_store"], "version", None)
            usage_revision = (self._usage_store._combined_stamp(), self._usage_store.utc_day())
            key = (selected_model_id, display_only, sources_revision, usage_revision)
            now = time.monotonic()
            self._catalog_cache = {k: v for k, v in self._catalog_cache.items()
                                   if now - v[0] < 2 and k[2:] == key[2:]}
            snapshot = self._catalog_cache.get(key)
        if snapshot is None:
            inputs = self._catalog_inputs(selected_model_id=selected_model_id, display_only=display_only)
            with self._catalog_lock:
                self._catalog_cache[key] = (now, inputs)
        else:
            inputs = snapshot[1]
        # Cache metadata inputs, never invocation authority. CLI bindings and
        # capabilities are rebuilt against the current provider on every call.
        return self._build_models(*inputs, selected_model_id=selected_model_id, display_only=display_only)

    def _catalog_inputs(self, *, selected_model_id=None, display_only=False):
        records = self.services["source_store"].read()
        try:
            discover = self.services.get("discover_agent", self.services["discover"])
            if display_only:
                discover = self.services.get("discover_display", discover)
            selected_discover = self.services.get("discover_local_model")
            if (isinstance(selected_model_id, str) and selected_model_id.startswith("local/")
                    and callable(selected_discover)):
                discovered = selected_discover(self.network, selected_model_id[6:].split("/model/", 1)[0])
            elif (isinstance(selected_model_id, str) and selected_model_id.startswith("local/")
                    and callable(self.services.get("discover_local"))):
                discover = self.services["discover_local"]
                discovered = discover(self.network)
            else:
                discovered = discover(self.network)
        except Exception as exc:
            with self._catalog_lock:
                self._diagnostics["discovery"] = {
                    "stage": "discover", "error_type": type(exc).__name__, "at": time.time()}
            discovered = []
        return records, discovered, self._usage_store.read_cached()

    def _build_models(self, records, discovered, usage_snapshot, *, selected_model_id=None, display_only=False):
        sources = self.services["source_store"]
        source_index = {sources.model_service_id(record, model): (record, model)
                        for record in records.values() for model in record["models"]}
        rows = {}
        for value in discovered:
            manifest = value.get("service") or {}
            service_id, peer_id = manifest.get("package_id"), value.get("peer_id")
            if not isinstance(service_id, str) or not isinstance(peer_id, str):
                continue
            own = peer_id == self.store.peer_id
            model_ref = ("local/" + service_id if own else
                         "peer/" + hashlib.sha256(peer_id.encode()).hexdigest() + "/" + service_id)
            provider_resource = value.get("provider_resource")
            if not own and provider_resource:
                model_ref = "provider/" + hashlib.sha256(provider_resource.encode()).hexdigest() + "/" + model_ref
            adapter = manifest.get("adapter", "")
            capabilities = manifest.get("capabilities") or []
            # Admission follows the bridge contract, not optional inference probes.
            # Older providers omitted client-tools when a probe was missing. A known
            # bridge plus the chat protocol still declares the same wire contract.
            declared = client_tools_contract(adapter, capabilities)
            supported = declared and value.get("chat_protocol") == "rynmesh.chat.v1"
            is_stale = value.get("catalog_state") == "stale"
            available = bool(
                not is_stale
                and value.get("available", True)
                and value.get("online")
                and value.get("ready", True)
            )
            is_cli = adapter in {"codex_cli", "antigravity_cli", "claude_cli"}
            local_tool_support = "unsupported" if adapter == "claude_cli" else value.get("tool_support", "unknown")
            if own and not is_cli and service_id not in source_index:
                caps = getattr(self.store, "llm_tool_capabilities", None)
                provider_for = self.services.get("provider_for") if isinstance(self.services, dict) else None
                if caps is not None and callable(provider_for):
                    mgr = provider_for(service_id)
                    if mgr is not None and getattr(mgr, "manifest", None) is not None:
                        stored_sup = caps.support(mgr.manifest)
                        if stored_sup != "unknown":
                            local_tool_support = stored_sup
                if local_tool_support == "unsupported":
                    supported = False
            declared_claude_window = (
                model_settings.known_vendor_context_metadata(manifest.get("model", ""))
                if adapter == "claude_cli" and manifest.get("model")
                else None
            )
            inferred_window = (
                model_settings.known_vendor_context_metadata(manifest.get("model_alias") or service_id)
                if not is_cli and type(manifest.get("context_window")) is not int else None
            )
            window = manifest.get("context_window", 32768)
            if not is_cli and type(window) is not int:
                window = 32768
            if declared_claude_window is not None:
                window = declared_claude_window["context_window"]
            elif inferred_window is not None:
                window = inferred_window["context_window"]
            vendor_window = declared_claude_window or inferred_window
            cli_basis = manifest.get("context_window_basis") if isinstance(manifest.get("context_window_basis"), str) else ""
            window_known = (
                (not is_cli and type(manifest.get("context_window")) is int and cli_basis not in {"vendor_family_guess", "unknown_fallback"})
                or bool(vendor_window and vendor_window["context_window_known"])
            )
            row = {"id": model_ref, "name": manifest.get("model_alias", service_id),
                   "source": "local_service" if own else "peer", "source_name": value.get("node_name", ""),
                   "service_id": service_id, "peer_id": peer_id, "adapter": adapter,
                   "runtime": manifest.get("runtime", ""),
                   "model_id": manifest.get("model_alias", service_id), "source_id": "",
                   "destination_revision": "",
                   "available": available, "agent_supported": supported,
                   **({"catalog_state": value["catalog_state"]} if "catalog_state" in value else {}),
                   "tool_support": local_tool_support,
                   "agent_support_basis": "adapter_contract" if own else "provider_protocol_contract",
                   **({"agent_support": "experimental"} if adapter == "antigravity_cli" else {}),
                   "context_window": window,
                   "context_window_known": window_known,
                   "context_window_basis": (
                       vendor_window["context_window_basis"]
                       if vendor_window is not None
                       else cli_basis
                       if cli_basis
                       else "provider_manifest"
                       if window_known
                       else (cli_basis or "unknown_fallback")
                   ),
                   "streaming": "streaming" in capabilities or "buffered-streaming" in capabilities,
                   "max_output_tokens": manifest.get("max_output_tokens", 4096),
                   "max_output_tokens_known": type(manifest.get("max_output_tokens")) is int,
                   "max_output_basis": "provider_manifest" if type(manifest.get("max_output_tokens")) is int else "unknown_fallback",
                   "sharing": bool(value.get("accepting_orders", value.get("publication_enabled", False))) if own else True,
                   "sharing_basis": "local_service_advertisement" if own else "remote_catalog_advertisement",
                   "sharing_configuration_known": False,
                   "share_scope": ("personal_space" if "personal-space-only" in capabilities else
                                   "friends" if value.get("access_policy") == "explicit_friends" else "none") if own else "unknown"}
            _model_facts(row, manifest)
            row.update(catalog_image_fields(row, service_record=value))
            if is_cli:
                _cli_output(row, {"id": manifest.get("model", "")})
            if not own and "context-estimate:v2" not in capabilities:
                # Legacy peer providers without context-estimate:v2 admit an order only
                # when its prompt's UTF-8 bytes plus output fit the window (1 byte/token).
                row["context_admission_bytes_per_token"] = 1
            if provider_resource:
                row.update(provider_resource=provider_resource, source_name=value.get("provider_name", value.get("node_name", "")),
                           provider_device_name=value.get("node_name", ""))
            rows[model_ref] = row
            if own and manifest.get("runtime") == "native_llama_cpp":
                native_binding = value.get("model_binding")
                row.update(local_model_binding=native_binding,
                           destination_binding_supported=peer_binding.valid(native_binding, service_id),
                           destination_revision=peer_binding.local_revision(native_binding, peer_id))
            if not own:
                bound = peer_binding.expected(value, self.store.peer_id)
                access = {"kind": "unknown", "consumer_peer_id": self.store.peer_id, "basis": "unknown",
                          "catalog_available": available, "current_access": "unknown", "invocation_verified": False}
                # Retain the authenticated observation for this recipient even
                # when the catalog marks it unavailable. That never establishes
                # current authorization, invocation success or full provider policy.
                if bound is not None:
                    permission = ("external_permission" if "external_permission" in bound else
                                  "ai_permission" if "ai_permission" in bound else "space_permission")
                    access.update(kind={"ai_permission": "friend", "space_permission": "personal_space",
                                        "external_permission": "provider"}[permission],
                                  basis="authenticated_catalog_observation", **{permission: dict(bound[permission])})
                row.update(destination_revision=peer_binding.revision(value, self.store.peer_id),
                           destination_binding_supported=bound is not None, expected_model_binding=bound,
                           consumer_peer_id=self.store.peer_id, recipient_access=access)
                # P2P still carries Ryn's canonical chat contract. A bound
                # source may also declare the native upstream continuation it
                # can replay; ambiguous or unbound claims never retain state.
                native_protocols = [protocol for protocol in ("openai_responses", "anthropic_messages")
                                    if "protocol:" + protocol in capabilities]
                if adapter == "openai_compatible" and bound is not None and len(native_protocols) == 1:
                    row["adapter"] = native_protocols[0]
                row.update(catalog_image_fields(row, service_record=value))
                if adapter in {"codex_cli", "antigravity_cli", "claude_cli"}:
                    from ..llm_package.cli_models import context_metadata
                    fallback_basis = row.get("context_window_basis", "unknown_fallback")
                    row.update(context_metadata(value.get("cli_models", []), fallback=row["context_window"], fallback_basis=fallback_basis))
                    _cli_output(row, next((model for model in value.get("cli_models", []) if model.get("default")), {}))
                    if row["context_window_known"]:
                        row["context_window_basis"] = "provider_cli_model_metadata"
                    for upstream in value.get("cli_models", []):
                        if not upstream.get("id"):
                            continue
                        specific = {**value, "model_binding": upstream.get("model_binding")}
                        child_bound = peer_binding.expected(specific, self.store.peer_id)
                        support = upstream.get("tool_support", "unknown")
                        identifier = model_ref + "/model/" + upstream["id"]
                        rows[identifier] = {**row, "id": identifier, "name": upstream["name"],
                            "cli_model": upstream["id"], "cli_source": adapter, "model_id": upstream["id"],
                            "tool_support": support, "agent_supported": row["agent_supported"],
                            "destination_revision": peer_binding.revision(specific, self.store.peer_id),
                            "destination_binding_supported": child_bound is not None, "expected_model_binding": child_bound}
                        rows[identifier].update(context_metadata([upstream], upstream["id"], fallback=row["context_window"], fallback_basis=fallback_basis))
                        _cli_output(rows[identifier], upstream)
                        if rows[identifier]["context_window_known"]:
                            rows[identifier]["context_window_basis"] = "provider_cli_model_metadata"
                        if adapter == "codex_cli":
                            rows[identifier].update(reasoning_metadata(upstream.get("supported_reasoning_efforts"), upstream.get("default_reasoning_effort")))
                        _model_facts(rows[identifier], upstream)
                        rows[identifier].update(catalog_image_fields(rows[identifier], service_record=value, cli_model_row=upstream))
        # Include drafts/offline sources so absence never masquerades as deletion.
        for service_id, (record, model) in source_index.items():
            model_ref = "local/" + service_id
            support = record.get("tool_support", {}).get(model, "unknown")
            prior = rows.get(model_ref, {})
            policy = model_settings.public(record, model)
            from . import model_profiles
            source_adapter = (record.get("protocol") if record.get("protocol") in {"openai_responses", "anthropic_messages"}
                              else "openai_compatible")
            verified_custom_tools = model_profiles.supports_custom_tools({
                "source": "local_api",
                "adapter": source_adapter,
                "provider": record.get("provider", ""),
                "base_url": record.get("base_url", ""),
                "model_id": model,
                "tool_support": support,
                "evidence": policy["evidence"],
            })
            rows[model_ref] = {**prior, "id": model_ref, "name": record["name"] + " · " + model,
                "source": "local_api", "source_name": record["name"], "source_id": record["id"],
                "provider": str(record.get("provider") or ""),
                "model_id": model, "service_id": service_id, "peer_id": self.store.peer_id,
                "destination_revision": self.source_destination_revision(record, model),
                "model_settings": policy, "rule_revision": policy.get("rule_revision", ""), "evidence": policy["evidence"],
                "adapter": source_adapter,
                **({"custom_tools": True} if verified_custom_tools else {}),
                "available": bool(prior.get("available") and record.get("enabled") and record.get("status") == "ready"),
                "availability_basis": "configured_source_readiness_not_live_probe",
                "streaming": record.get("streaming_support", {}).get(model) == "supported",
                "streaming_basis": "source_probe" if model in record.get("streaming_support", {}) else "unverified",
                "agent_supported": True, "tool_support": support, "agent_support_basis": "adapter_contract",
                "context_window": policy["effective"]["context_window"], "max_output_tokens": policy["effective"]["max_output_tokens"],
                "max_output_tokens_known": policy["effective"]["max_output_tokens_known"],
                "max_output_basis": policy["effective"]["max_output_basis"],
                "context_window_basis": policy["context_window_basis"], "context_window_known": policy["context_window_known"],
                "sharing": model in sources.shared_models(record), "share_scope": policy["effective"]["share_scope"],
                "sharing_basis": "local_configuration", "sharing_configuration_known": True,
                "destination_binding_supported": True}
            rows[model_ref].update(reasoning_metadata(
                policy["effective"].get("supported_reasoning_efforts"),
                policy["effective"].get("request_defaults", {}).get("reasoning_effort")))
            rows[model_ref].update(catalog_image_fields(rows[model_ref], source_record=record))
        # CLI sources are containers; each upstream model keeps its identity and diagnostics.
        for row in list(rows.values()):
            if row["source"] != "local_service" or row["adapter"] not in {"codex_cli", "antigravity_cli"}:
                continue
            # Task admission and context sizing need one current model. A cold
            # unrelated CLI can take seconds to enumerate, even for API tasks.
            if selected_model_id is not None and selected_model_id != row["id"] and not selected_model_id.startswith(row["id"] + "/model/"):
                continue
            row["name"] += " · CLI default"
            try:
                from ..llm_package.cli_adapter import CLIAgentAdapter
                from ..llm_package.cli_models import cached_models, list_models
                adapter = CLIAgentAdapter(row["adapter"])
                executable = adapter._command_path()
                manager = self.services["provider_for"](row["service_id"])
                with self._cli_metadata_lock:
                    snapshot = self._cli_metadata.get(row["id"])
                prefix = row["id"] + "/model/"
                selected = selected_model_id[len(prefix):] if selected_model_id and selected_model_id.startswith(prefix) else None
                if not row["available"] and snapshot and snapshot["manager"] is manager:
                    # Offline display metadata conveys no current project authority.
                    upstream = copy.deepcopy(snapshot["models"])
                    row.update(local_model_binding=None, destination_binding_supported=False, destination_revision="")
                else:
                    current_models = getattr(manager.adapter, "display_models" if display_only else "current_models", None) if manager else None
                    upstream = (current_models() if callable(current_models) else
                                cached_models(row["adapter"], executable) or list_models(row["adapter"], executable))
                    self._bind_cli(row, manager, cached=display_only)
                    with self._cli_metadata_lock:
                        self._cli_metadata[row["id"]] = {"manager": manager, "models": copy.deepcopy(upstream)}
            except Exception as exc:
                with self._catalog_lock:
                    self._diagnostics.setdefault("cli_models", {})[row["id"]] = {
                        "stage": "cli_models", "source": row["id"],
                        "error_type": type(exc).__name__, "at": time.time()}
                row.update(available=False, local_model_binding=None,
                           destination_binding_supported=False, destination_revision="")
                continue
            for model in upstream:
                if not model["id"] or (selected and model["id"] != selected):
                    continue
                support = self.store.llm_tool_capabilities.support(manager.manifest, model["id"]) if manager else "unknown"
                identifier = row["id"] + "/model/" + model["id"]
                rows[identifier] = {**row, "id": identifier, "name": model["name"],
                    "cli_model": model["id"], "cli_source": row["adapter"], "model_id": model["id"],
                    "source_name": "Codex" if row["adapter"] == "codex_cli" else "Antigravity",
                    "tool_support": support, "agent_supported": row["agent_supported"],
                    **({"agent_support": model["agent_support"]} if model.get("agent_support") else {})}
                from ..llm_package.cli_models import context_metadata
                local_fallback_basis = row.get("context_window_basis", "unknown_fallback")
                rows[identifier].update(context_metadata([model], model["id"], fallback=row["context_window"], fallback_basis=local_fallback_basis))
                _cli_output(rows[identifier], model)
                if row["available"]:
                    self._bind_cli(rows[identifier], manager, model["id"], cached=display_only)
                if row["adapter"] == "codex_cli":
                    rows[identifier].update(reasoning_metadata(model.get("supported_reasoning_efforts"), model.get("default_reasoning_effort")))
                _model_facts(rows[identifier], model)
                rows[identifier].update(catalog_image_fields(rows[identifier], cli_model_row=model))
            default = next((model for model in upstream if model.get("default")), {})
            if row["adapter"] == "codex_cli":
                row.update(reasoning_metadata(default.get("supported_reasoning_efforts"), default.get("default_reasoning_effort")))
            _model_facts(row, default)
            row.update(catalog_image_fields(row, cli_model_row=default))
            from ..llm_package.cli_models import context_metadata
            row.update(context_metadata(upstream, fallback=row["context_window"], fallback_basis=row.get("context_window_basis", "unknown_fallback")))
            _cli_output(row, default)
        usage_store = self._usage_store
        space_catalog = self.services.get("space_catalog") if isinstance(self.services, dict) else None
        last_errors = getattr(space_catalog, "last_errors", None)
        for row in rows.values():
            with self._cli_metadata_lock:
                learned = self._learned_output_limits.get((row["id"], row.get("destination_revision", "")))
                learned_context = self._learned_context_limits.get((row["id"], row.get("destination_revision", "")))
            if learned is not None and learned < row.get("max_output_tokens", 4096):
                row.update(max_output_tokens=learned, max_output_tokens_known=True, max_output_basis="upstream_rejection")
            if learned_context is not None:
                row.update(context_window=learned_context, context_window_known=True, context_window_basis="upstream_rejection")
            ms = row.get("model_settings")
            policy = (
                ms.get("effective")
                if isinstance(ms, dict) and isinstance(ms.get("effective"), dict)
                else ms.get("configuration")
                if isinstance(ms, dict) and isinstance(ms.get("configuration"), dict)
                else None
            )
            usage = usage_store.public_usage(row["id"], policy=policy, snapshot=usage_snapshot)
            row["usage"] = usage
            if isinstance(ms, dict):
                ms["usage"] = usage
            peer_err = None
            if row["source"] == "peer" and isinstance(last_errors, dict):
                entry = last_errors.get(row.get("peer_id", ""))
                if isinstance(entry, tuple) and entry:
                    peer_err = str(entry[0])
                elif isinstance(entry, str) and entry:
                    peer_err = entry
            if peer_err and not row["available"]:
                row["catalog_refresh_error"] = peer_err
                row["description"] = f"目录刷新失败：{_format_catalog_error(peer_err)}"
            elif row.get("catalog_state") == "stale":
                row["description"] = "待确认（目录缓存已过期，使用前将自动刷新）"
            else:
                row["description"] = ("模型当前不可用，请检查来源连接。" if not row["available"] else
                    "可供本机 Agent 使用；工具在本机执行。" if row["agent_supported"] else
                    "此接入只支持对话，不能执行 Agent 工具。" if row["adapter"] == "claude_cli" else
                    "此接入未声明助手工具协议支持。")
        return sorted(rows.values(), key=lambda row: (row["source"] == "peer", row["name"], row["id"]))

    def get_model(self, model_id):
        rows = self.list_models(selected_model_id=model_id)
        return next((row for row in rows if row["id"] == model_id), None)

    def display_models(self):
        return self.list_models(display_only=True)

    def display_model(self, model_id):
        return next((row for row in self.list_models(selected_model_id=model_id, display_only=True)
                     if row["id"] == model_id), None)

    def refresh_destination(self, model_id):
        """Refresh metadata only; never send task text to repair discovery."""
        with self._catalog_lock:
            self._catalog_cache.clear()
        try:
            records = self.services["discover"](self.network)
        except Exception as exc:
            diagnostic_log.failure("discovery_failed", exc)
            raise ValueError("agent_model_unavailable") from None
        locator = next((row for row in records if row.get("peer_id")
            and model_id.startswith("peer/" + hashlib.sha256(row["peer_id"].encode()).hexdigest() + "/")
            and row.get("access_policy") == "personal_space"), None)
        space_catalog = self.services.get("space_catalog") if isinstance(self.services, dict) else None
        refresh_error = None
        if locator is not None and space_catalog is not None and self.store.personal_space.allows_ai(locator["peer_id"]):
            peer_id = locator["peer_id"]
            try:
                space_catalog.refresh(locator)
            except Exception as exc:
                last_errors = getattr(space_catalog, "last_errors", None)
                entry = last_errors.get(peer_id) if isinstance(last_errors, dict) else None
                if isinstance(entry, tuple) and entry:
                    refresh_error = str(entry[0])
                elif str(exc).startswith("space_catalog_"):
                    refresh_error = str(exc)
                else:
                    refresh_error = "space_catalog_refresh_failed"
                if isinstance(last_errors, dict) and peer_id not in last_errors:
                    last_errors[peer_id] = (refresh_error, 0)
        with self._catalog_lock:
            self._catalog_cache.clear()
        row = self.get_model(model_id)
        if row is not None and refresh_error and not row.get("available"):
            row["catalog_refresh_error"] = refresh_error
            row["description"] = f"目录刷新失败：{_format_catalog_error(refresh_error)}"
        return row

    def resolve(self, model_id, *, require_tools=True):
        row = self.get_model(model_id)
        if model_id.startswith("peer/") and (
            not row
            or not row.get("destination_binding_supported")
            or row.get("catalog_state") == "stale"
        ):
            row = self.refresh_destination(model_id)
        if not row or not row["available"]:
            raise ValueError("agent_model_unavailable")
        if require_tools and not row["agent_supported"]:
            raise ValueError("agent_model_tools_unsupported")
        return row
