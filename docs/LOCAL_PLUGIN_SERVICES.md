# Managed local plugin services

A local plugin needs both its web UI and a runnable backend. A loopback `webApp.url` alone does not install or start a service.

The desktop loads `plugin-services/services.json` beside `Ryn.exe`. This file is installed with trusted native runtime files, separately from imported web manifests. Each service contains an executable relative to `plugin-services`, the plugin IDs sharing it, a loopback port, a health endpoint, and an optional environment map. The service program must remain inside that directory after resolving links. No command line or executable is accepted from an iframe or a web manifest.

```json
{
  "version": 1,
  "services": [{
    "id": "reports",
    "plugins": ["reports", "charts"],
    "program": "reports/service.exe",
    "port": 18140,
    "health_path": "/health",
    "environment": {}
  }]
}
```

Ryn checks the installed plugin's enabled state and exact loopback origin before starting a service. It starts enabled services with the desktop, waits up to 30 seconds for HTTP health, and serializes concurrent opens so shared backends run once. Opening a plugin also requests readiness before mounting its iframe. If the process exits, the supervisor retries with a 30-second interval; disabling or removing all dependent plugins stops it. An occupied port is reported as a conflict, never adopted or killed.

Desktop exit closes owned services. Windows uses a kill-on-close Job Object so a desktop crash also closes the process tree. The service log is in the desktop log directory as `plugin-<id>.log`. Existing remote HTTPS applications continue to run without a local process.

The supervisor reloads the registry on each polling cycle, including when no services were installed at desktop startup. A changed service declaration replaces the owned runtime on its next check. Invalid registry updates leave running services alone. A live owned process with failing health checks receives a 90-second grace period before replacement; a successful check resets that period. The plugin error page retries transient failures every 10 seconds and distinguishes port conflicts from missing runtime files. Foreign port owners are never terminated automatically.

The company service is built in `chat-backend/integrations/company-app` with `scripts/build_desktop_service.py`. It includes its interpreter, dependencies, web assets and declaration files. Private configuration, databases and encryption keys are excluded. Its installer registers the shared runtime for Gshopper, Dashboard and Affiliate and points `COMPANY_APP_HOME` at the existing data directory. The runtime package is independent of the core Ryn installer and must be retained during core updates.
