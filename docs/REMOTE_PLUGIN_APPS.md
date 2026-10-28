# Remote plugin applications

Imported packages can declare a remote user interface with `ryn.webApp`:

```json
{
  "id": "reports",
  "name": "Reports",
  "ryn": {
    "webApp": {
      "url": "https://app.example.test/workbench",
      "permissions": ["tools", "agent"]
    }
  }
}
```

The application runs on a separate origin in a sandboxed frame at `/apps/extensions/:pluginId`. Import defaults to disabled. HTTPS is required except for loopback development. The import preview and settings show the origin and requested capabilities.

The frame can post `{type: 'ryn.plugin.request', id, method, args}` to its parent using the exact `host_origin` query parameter. Responses use `{type: 'ryn.plugin.response', id, result}` or a generic `error`. The host checks the source window, origin, current plugin enabled state, current app URL and declared permissions before each dispatch. Messages cannot request arbitrary HTTP calls or choose another plugin's tool credentials.

| Permission | Methods |
| --- | --- |
| tools | tools.query(toolName, arguments) |
| agent | agent.draft(prompt), opens an editable draft without sending |
| models | models.list, models.access (no keys), models.share(serviceId, enabled) |
| messages | messages.list(peerId), messages.send(payload) |
| peer_provider | peer.status, peer.workspace, peer.sync, peer.bind, peer.catalog, peer.refresh, peer.models, peer.publish(serviceIds) |

Tool calls reuse the existing plugin tool-query route and its read-only, version, authorization and data-policy checks. Credentials remain in the host. Explicit `ryn.identity_group` metadata can group OAuth sessions only when the resource, client identifier and requested scopes also match. Product-specific identity rules, backend code and database drivers belong to the external application.
