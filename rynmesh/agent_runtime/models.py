"""Credential-free model pool, independent of local Agent preferences."""
from __future__ import annotations

import copy
import hashlib
import os
import threading

from ..llm_package import model_settings, peer_binding
from ..llm_package.reasoning import metadata as reasoning_metadata
from ..llm_package.tool_capabilities import client_tools_contract
from .image_capabilities import catalog_image_fields


class ModelCatalog:
    def __init__(self, services, store):
        self.services, self.store = services, store
        self.network = os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main")
        # Only CLI model metadata is retained. Availability and data authority
        # are rebuilt from the current provider and exact model binding.
        self._cli_metadata = {}
        self._cli_metadata_lock = threading.Lock()

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
        sources = self.services["source_store"]
        records = sources.read()
        source_index = {sources.model_service_id(record, model): (record, model)
                        for record in records.values() for model in record["models"]}
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
        except Exception:
            discovered = []
        rows = {}
        for value in discovered:
            manifest = value.get("service") or {}
            service_id, peer_id = manifest.get("package_id"), value.get("peer_id")
            if not isinstance(service_id, str) or not isinstance(peer_id, str):
                continue
            own = peer_id == self.store.peer_id
            model_ref = ("local/" + service_id if own else
                         "peer/" + hashlib.sha256(peer_id.encode()).hexdigest() + "/" + service_id)
            adapter = manifest.get("adapter", "")
            capabilities = manifest.get("capabilities") or []
            # Admission follows the bridge contract, not optional inference probes.
            # Older providers omitted client-tools when a probe was missing. A known
            # bridge plus the chat protocol still declares the same wire contract.
            declared = client_tools_contract(adapter, capabilities)
            supported = declared and value.get("chat_protocol") == "rynmesh.chat.v1"
            available = bool(value.get("online") and value.get("ready", True))
            row = {"id": model_ref, "name": manifest.get("model_alias", service_id),
                   "source": "local_service" if own else "peer", "source_name": value.get("node_name", ""),
                   "service_id": service_id, "peer_id": peer_id, "adapter": adapter,
                   "runtime": manifest.get("runtime", ""),
                   "model_id": manifest.get("model_alias", service_id), "source_id": "",
                   "destination_revision": "",
                   "available": available, "agent_supported": supported,
                   "tool_support": "unsupported" if adapter == "claude_cli" else value.get("tool_support", "unknown"),
                   "agent_support_basis": "adapter_contract" if own else "provider_protocol_contract",
                   "context_window": manifest.get("context_window", 32768),
                   "context_window_known": adapter not in {"codex_cli", "antigravity_cli"} and type(manifest.get("context_window")) is int,
                   "context_window_basis": "provider_manifest" if adapter not in {"codex_cli", "antigravity_cli"} and type(manifest.get("context_window")) is int else "unknown_fallback",
                   "streaming": "streaming" in capabilities or "buffered-streaming" in capabilities,
                   "max_output_tokens": manifest.get("max_output_tokens", 4096),
                   "sharing": bool(value.get("accepting_orders", value.get("publication_enabled", False))) if own else True,
                   "sharing_basis": "local_service_advertisement" if own else "remote_catalog_advertisement",
                   "sharing_configuration_known": False,
                   "share_scope": ("personal_space" if "personal-space-only" in capabilities else
                                   "friends" if value.get("access_policy") == "explicit_friends" else "none") if own else "unknown"}
            row.update(catalog_image_fields(row, service_record=value))
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
                    permission = "ai_permission" if "ai_permission" in bound else "space_permission"
                    access.update(kind="friend" if permission == "ai_permission" else "personal_space",
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
                if adapter in {"codex_cli", "antigravity_cli"}:
                    from ..llm_package.cli_models import context_metadata
                    row.update(context_metadata(value.get("cli_models", []), fallback=row["context_window"]))
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
                        rows[identifier].update(context_metadata([upstream], upstream["id"], fallback=row["context_window"]))
                        max_output = upstream.get("max_output_tokens")
                        if not isinstance(max_output, int) and rows[identifier].get("context_window_known"):
                            max_output = model_settings._known_vendor_max_output_tokens(upstream["id"])
                        if isinstance(max_output, int) and 0 < max_output <= rows[identifier]["context_window"]:
                            rows[identifier]["max_output_tokens"] = max_output
                        if rows[identifier]["context_window_known"]:
                            rows[identifier]["context_window_basis"] = "provider_cli_model_metadata"
                        if adapter == "codex_cli":
                            rows[identifier].update(reasoning_metadata(upstream.get("supported_reasoning_efforts"), upstream.get("default_reasoning_effort")))
                        rows[identifier].update(catalog_image_fields(rows[identifier], service_record=value, cli_model_row=upstream))
        # Include drafts/offline sources so absence never masquerades as deletion.
        for service_id, (record, model) in source_index.items():
            model_ref = "local/" + service_id
            support = record.get("tool_support", {}).get(model, "unknown")
            prior = rows.get(model_ref, {})
            policy = model_settings.public(record, model)
            rows[model_ref] = {**prior, "id": model_ref, "name": record["name"] + " · " + model,
                "source": "local_api", "source_name": record["name"], "source_id": record["id"],
                "model_id": model, "service_id": service_id, "peer_id": self.store.peer_id,
                "destination_revision": self.source_destination_revision(record, model),
                "model_settings": policy, "rule_revision": policy.get("rule_revision", ""), "evidence": policy["evidence"],
                "adapter": (record.get("protocol") if record.get("protocol") in {"openai_responses", "anthropic_messages"}
                            else "openai_compatible"),
                "available": bool(prior.get("available") and record.get("enabled") and record.get("status") == "ready"),
                "availability_basis": "configured_source_readiness_not_live_probe",
                # A source's two-turn tool probe proves tools, not streaming.
                "streaming": False,
                "agent_supported": True, "tool_support": support, "agent_support_basis": "adapter_contract",
                "context_window": policy["effective"]["context_window"], "max_output_tokens": policy["effective"]["max_output_tokens"],
                "context_window_basis": policy["context_window_basis"], "context_window_known": policy["context_window_known"],
                "sharing": model in sources.shared_models(record), "share_scope": policy["effective"]["share_scope"],
                "sharing_basis": "local_configuration", "sharing_configuration_known": True,
                "destination_binding_supported": True}
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
            except Exception:
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
                    "tool_support": support, "agent_supported": row["agent_supported"]}
                from ..llm_package.cli_models import context_metadata
                rows[identifier].update(context_metadata([model], model["id"], fallback=row["context_window"]))
                max_output = model.get("max_output_tokens")
                if not isinstance(max_output, int) and rows[identifier].get("context_window_known"):
                    max_output = model_settings._known_vendor_max_output_tokens(model["id"])
                if isinstance(max_output, int) and 0 < max_output <= rows[identifier]["context_window"]:
                    rows[identifier]["max_output_tokens"] = max_output
                if row["available"]:
                    self._bind_cli(rows[identifier], manager, model["id"], cached=display_only)
                if row["adapter"] == "codex_cli":
                    rows[identifier].update(reasoning_metadata(model.get("supported_reasoning_efforts"), model.get("default_reasoning_effort")))
                rows[identifier].update(catalog_image_fields(rows[identifier], cli_model_row=model))
            default = next((model for model in upstream if model.get("default")), {})
            if row["adapter"] == "codex_cli":
                row.update(reasoning_metadata(default.get("supported_reasoning_efforts"), default.get("default_reasoning_effort")))
            row.update(catalog_image_fields(row, cli_model_row=default))
            from ..llm_package.cli_models import context_metadata
            row.update(context_metadata(upstream, fallback=row["context_window"]))
            default_output = default.get("max_output_tokens")
            if not isinstance(default_output, int) and row.get("context_window_known"):
                default_output = model_settings._known_vendor_max_output_tokens(default.get("id", ""))
            if isinstance(default_output, int) and 0 < default_output <= row["context_window"]:
                row["max_output_tokens"] = default_output
        for row in rows.values():
            row["description"] = ("模型当前不可用，请检查来源连接。" if not row["available"] else
                "可供本机 Agent 使用；工具在本机执行。" if row["agent_supported"] else
                "此接入只支持对话，不能执行 Agent 工具。" if row["adapter"] == "claude_cli" else
                "此接入未声明助手工具协议支持。")
        return sorted(rows.values(), key=lambda row: (row["source"] == "peer", row["name"], row["id"]))

    def get_model(self, model_id):
        try:
            rows = self.list_models(selected_model_id=model_id)
        except TypeError:
            rows = self.list_models()
        return next((row for row in rows if row["id"] == model_id), None)

    def display_models(self):
        return self.list_models(display_only=True)

    def display_model(self, model_id):
        return next((row for row in self.list_models(selected_model_id=model_id, display_only=True)
                     if row["id"] == model_id), None)

    def refresh_destination(self, model_id):
        """Refresh metadata only; never send task text to repair discovery."""
        records = self.services["discover"](self.network)
        locator = next((row for row in records if row.get("peer_id")
            and model_id.startswith("peer/" + hashlib.sha256(row["peer_id"].encode()).hexdigest() + "/")
            and row.get("access_policy") == "personal_space"), None)
        if locator is not None and self.store.personal_space.allows_ai(locator["peer_id"]):
            try:
                self.services["space_catalog"].refresh(locator)
            except Exception:
                pass  # No stale record is promoted to authenticated authority.
        return self.get_model(model_id)

    def resolve(self, model_id, *, require_tools=True):
        row = self.get_model(model_id)
        if model_id.startswith("peer/") and (not row or not row.get("destination_binding_supported")):
            row = self.refresh_destination(model_id)
        if not row or not row["available"]:
            raise ValueError("agent_model_unavailable")
        if require_tools and not row["agent_supported"]:
            raise ValueError("agent_model_tools_unsupported")
        return row
