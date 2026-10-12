"""Credential-free model pool, independent of local Agent preferences."""
from __future__ import annotations

import hashlib
import os

from ..llm_package import model_settings, peer_binding


class ModelCatalog:
    def __init__(self, services, store):
        self.services, self.store = services, store
        self.network = os.environ.get("RYNMESH_NETWORK_ID", "rynmesh-main")

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

    def list_models(self):
        sources = self.services["source_store"]
        records = sources.read()
        source_index = {sources.model_service_id(record, model): (record, model)
                        for record in records.values() for model in record["models"]}
        try:
            discovered = self.services["discover"](self.network)
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
            # Protocol support alone does not demonstrate model tool ability.
            supported = "client-tools" in capabilities and value.get("chat_protocol") == "rynmesh.chat.v1"
            if adapter == "claude_cli":
                supported = False
            if adapter in {"codex_cli", "antigravity_cli"}:
                supported = supported and value.get("tool_support") == "supported"
            available = bool(value.get("online") and value.get("ready", True))
            row = {"id": model_ref, "name": manifest.get("model_alias", service_id),
                   "source": "local_service" if own else "peer", "source_name": value.get("node_name", ""),
                   "service_id": service_id, "peer_id": peer_id, "adapter": adapter,
                   "runtime": manifest.get("runtime", ""),
                   "model_id": manifest.get("model_alias", service_id), "source_id": "",
                   "destination_revision": "",
                   "available": available, "agent_supported": supported,
                   "tool_support": "supported" if supported else "unsupported" if adapter == "claude_cli" else value.get("tool_support", "unknown"),
                   "context_window": manifest.get("context_window", 32768),
                   "streaming": "streaming" in capabilities or "buffered-streaming" in capabilities,
                   "max_output_tokens": manifest.get("max_output_tokens", 4096),
                   "sharing": bool(value.get("accepting_orders", value.get("publication_enabled", False))) if own else True,
                   "sharing_basis": "local_service_advertisement" if own else "remote_catalog_advertisement",
                   "sharing_configuration_known": False,
                   "share_scope": ("personal_space" if "personal-space-only" in capabilities else
                                   "friends" if value.get("access_policy") == "explicit_friends" else "none") if own else "unknown"}
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
                "adapter": "openai_compatible", "available": bool(prior.get("available") and record.get("enabled") and record.get("status") == "ready"),
                "availability_basis": "configured_source_readiness_not_live_probe",
                # A source's two-turn tool probe proves tools, not streaming.
                "streaming": False,
                "agent_supported": support == "supported", "tool_support": support,
                "context_window": policy["effective"]["context_window"], "max_output_tokens": policy["effective"]["max_output_tokens"],
                "sharing": model in sources.shared_models(record), "share_scope": policy["effective"]["share_scope"],
                "sharing_basis": "local_configuration", "sharing_configuration_known": True,
                "destination_binding_supported": True}
        for row in rows.values():
            row["description"] = ("模型当前不可用，请检查来源连接。" if not row["available"] else
                "可供本机 Agent 使用；工具在本机执行。" if row["agent_supported"] else
                "此接入只支持对话，不能执行 Agent 工具。" if row["adapter"] == "claude_cli" else
                "Codex CLI 尚未通过隔离客户端工具验证；当前版本可能禁止无执行环境的工具调用，普通推理仍可用。" if row["adapter"] == "codex_cli" else
                "尚未通过工具调用验证，可重试；暂不能用于助手。" if row["tool_support"] == "unsupported" else
                "尚未验证结构化工具调用，请先验证此模型。")
        return sorted(rows.values(), key=lambda row: (row["source"] == "peer", row["name"], row["id"]))

    def resolve(self, model_id, *, require_tools=True):
        row = next((row for row in self.list_models() if row["id"] == model_id), None)
        if not row or not row["available"]:
            raise ValueError("agent_model_unavailable")
        if require_tools and not row["agent_supported"]:
            raise ValueError("agent_model_tools_unsupported")
        return row
