import { tr, useUILanguage } from "./uiI18n";
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Navigate, Outlet, Route, Routes, useNavigate } from "react-router-dom";
import { AlertTriangle, RotateCcw } from "lucide-react";
import { ConfirmDialog, LoadingPanel, Toast } from "./components/ui";
import { RynLockup } from "./brand/RynBrand";
import OnboardingTour, {
  ONBOARDING_VERSION,
} from "./components/OnboardingTour";
import type { AppOutletContext } from "./appContext";
import type { NodeClient } from "./domain/nodeClient";
import { digestApi } from "./domain/digestClient";
import { makeFixtureNodeClient } from "./domain/fixtureNodeClient";
import { makeLiveNodeClient } from "./domain/liveNodeClient";
import { nodeControlBaseUrl } from "./domain/nodeUrl";
import {
  installNotificationNavigation,
  sendDiscoveryNotification,
} from "./domain/notifications";
import type {
  ConfirmRequest,
  NodeSettings,
  NodeStatus,
  Peer,
  RegistryStatus,
  ToastMessage,
} from "./domain/types";
import {
  PersonalProvider,
  applyAppearance,
  readAppearance,
} from "./personal/model";
import { DesktopPage } from "./personal/Desktop";
import { isTauriDesktop } from "./domain/nodeUrl";
import { PersonalShell } from "./personal/Shell";
import { PersonalSpacePage } from "./personal/Space";
import {
  PersonalHome,
  PersonalServices,
  PersonalDevices,
  PersonalTasks,
  PersonalSettings,
} from "./personal/Pages";
import Digest from "./screens/Digest";
import Explore from "./screens/Explore";
import ItemDetail from "./screens/ItemDetail";
import SearchAsk from "./screens/SearchAsk";
import Publish from "./screens/Publish";
import Peers from "./screens/Peers";
import Services from "./screens/Services";
import ServicesCatalog from "./screens/ServicesCatalog";
import PrivateAIChat from "./screens/PrivateAIChat";
import VideoRendering from "./screens/VideoRendering";
import SecureWebAccess from "./screens/SecureWebAccess";
import Chat from "./screens/Chat";
import Settings from "./screens/Settings";
import { APIAccessPage, AgentSharingPage, ModelMappingPage } from "./screens/AIWorkspacePages";
import AISources from "./screens/AISources";
import UnlockGate from "./screens/components/UnlockGate";
const NasPage = lazy(() => import("./personal/Nas").then(m => ({ default: m.NasPage })));
const PluginsPage = lazy(() => import("./personal/Nas").then(m => ({ default: m.PluginsPage })));

// Resolve the local Ryn node control-API base.
// Precedence: explicit env override > packaged-desktop default > dev default.
// - Dev (browser + Vite): undefined -> liveNodeClient uses "/api/local",
//   which the Vite dev proxy forwards to the daemon. No proxy exists in a
//   packaged build, so the Tauri shell must address the daemon directly.
function makeClient(): NodeClient {
  const params = new URLSearchParams(window.location.search);
  const explicit = params.get("client") ?? import.meta.env.VITE_RYN_NODE_CLIENT;
  if (explicit === "fixture") {
    return makeFixtureNodeClient();
  }
  return makeLiveNodeClient(nodeControlBaseUrl());
}

export default function App() {
  const uiLanguage = useUILanguage();
  const navigate = useNavigate();
  const client = useMemo(makeClient, []);
  const [node, setNode] = useState<NodeStatus | null>(null);
  const [registry, setRegistry] = useState<RegistryStatus | null>(null);
  const [peers, setPeers] = useState<Peer[]>([]);
  const [settings, setSettings] = useState<NodeSettings | null>(null);
  const [offline, setOffline] = useState(false);
  const [booting, setBooting] = useState(true);
  const [tourOpen, setTourOpen] = useState(false);
  const tourEvaluated = useRef(false);
  const [confirmRequest, setConfirmRequest] = useState<ConfirmRequest | null>(
    null,
  );
  const [toast, setToast] = useState<ToastMessage | null>(null);
  const lastUnread = useRef(0);

  const notify = useCallback((tone: ToastMessage["tone"], text: string) => {
    const message = { id: crypto.randomUUID(), tone, text };
    setToast(message);
    window.setTimeout(() => {
      setToast((current) => (current?.id === message.id ? null : current));
    }, 3200);
  }, []);

  const refreshShell = useCallback(async () => {
    try {
      const quickPeers = client.listPeers({ quick: true }).catch(() => null);
      const [nodeStatus, nodeSettings] =
        await Promise.all([
          client.getNodeStatus(),
          client.getSettings(),
        ]);
      setNode(nodeStatus);
      setRegistry({ status: nodeStatus.registry === "connected" ? "connected" : "disconnected",
        url: nodeSettings.registry_url });
      void client.getRegistryStatus().then(setRegistry).catch(() => undefined);
      const peerList = await quickPeers;
      if (peerList) setPeers(peerList);
      setSettings(nodeSettings);
      if (!tourEvaluated.current) {
        tourEvaluated.current = true;
        setTourOpen(
          client.mode === "live" &&
            nodeSettings.onboarding_version < ONBOARDING_VERSION,
        );
      }
      setOffline(false);
    } catch {
      setOffline(true);
    }
  }, [client]);

  useEffect(() => {
    if (!node) return;
    let cancelled = false;
    let fullTimer: number | undefined;
    const started = Date.now();
    const setIfChanged = (incoming: Peer[], merge: boolean) => {
      if (cancelled) return;
      setPeers(current => {
        const next = merge
          ? Array.from(new Map([...current, ...incoming].map(peer => [peer.id, peer])).values())
          : incoming;
        return JSON.stringify(next) === JSON.stringify(current) ? current : next;
      });
    };
    const quick = () => {
      void client.listPeers({ quick: true }).then(items => setIfChanged(items, true)).catch(() => undefined);
    };
    const full = async () => {
      try { setIfChanged(await client.listPeers(), false); }
      catch { /* Keep the latest local or cached discovery. */ }
      if (!cancelled) fullTimer = window.setTimeout(full, Date.now() - started < 30000 ? 3000 : 15000);
    };
    quick();
    void full();
    const quickTimer = window.setInterval(quick, 2000);
    return () => { cancelled = true; window.clearInterval(quickTimer); window.clearTimeout(fullTimer); };
  }, [client, node?.peer_id]);

  useEffect(() => {
    applyAppearance(readAppearance());
    let cancelled = false;
    void (async () => {
      // The packaged desktop app launches the Ryn node as a sidecar; it can
      // take several seconds to unpack and accept connections on a cold start.
      // Keep the loading state until the node is ready instead of flashing an
      // offline shell just before its first successful response.
      const maxAttempts = 60;
      for (let attempt = 0; attempt < maxAttempts && !cancelled; attempt += 1) {
        try {
          await client.getNodeStatus();
          break;
        } catch {
          if (attempt === maxAttempts - 1) break;
          await new Promise((resolve) => setTimeout(resolve, 800));
        }
      }
      if (cancelled) return;
      await refreshShell();
      if (!cancelled) setBooting(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [client, refreshShell]);

  useEffect(() => {
    if (client.mode !== "live") return;
    let active = true;
    const update = () => {
      void digestApi
        .getDiscoveryStatus()
        .then((status) => {
          if (!active) return;
          if (settings && status.unread_count > lastUnread.current) {
            void sendDiscoveryNotification(settings, status.unread_count, () =>
              navigate("/digest"),
            );
          }
          lastUnread.current = status.unread_count;
        })
        .catch(() => undefined);
    };
    update();
    const timer = window.setInterval(update, 5000);
    const seen = () => update();
    window.addEventListener("ryn-discovery-seen", seen);
    return () => {
      active = false;
      window.clearInterval(timer);
      window.removeEventListener("ryn-discovery-seen", seen);
    };
  }, [client, navigate, settings]);

  useEffect(() => {
    let disposed = false;
    let remove: () => void = () => {};
    void installNotificationNavigation(() => navigate("/digest"))
      .then((unregister) => {
        if (disposed) unregister();
        else remove = unregister;
      })
      .catch(() => undefined);
    return () => { disposed = true; remove(); };
  }, [navigate, uiLanguage]);

  if (!node || !registry || !settings) {
    if (booting) return <LoadingPanel />;
    if (offline) return <OfflineShell onRetry={refreshShell} />;
    return <LoadingPanel />;
  }

  const context: AppOutletContext = {
    client,
    node,
    registry,
    peers,
    refreshShell,
    confirm: setConfirmRequest,
    notify,
  };

  return (
    <PersonalProvider
      client={client}
      node={node}
      peers={peers}
      refreshShell={refreshShell}
    >
      <PersonalShell>
        {offline ? <OfflineBanner onRetry={refreshShell} /> : null}
        <Outlet context={context} />
      </PersonalShell>
      <ConfirmDialog
        request={confirmRequest}
        onCancel={() => setConfirmRequest(null)}
      />
      <Toast toast={toast} onDismiss={() => setToast(null)} />
      {tourOpen ? (
        <OnboardingTour
          node={node}
          settings={settings}
          peers={peers}
          onClose={() => setTourOpen(false)}
          onComplete={async () => {
            const updated = await client.updateSettings({
              onboarding_version: ONBOARDING_VERSION,
            });
            setSettings(updated);
            setTourOpen(false);
          }}
        />
      ) : null}
    </PersonalProvider>
  );
}

function OfflineBanner({ onRetry }: { onRetry: () => Promise<void> }) {
  useUILanguage();
  return (
    <div className="offline-banner">
      <AlertTriangle size={16} />
      {tr("Cannot connect to this device.")}
      <button type="button" onClick={() => void onRetry()}>
        {tr("Retry")}
      </button>
    </div>
  );
}

function OfflineShell({ onRetry }: { onRetry: () => Promise<void> }) {
  useUILanguage();
  return (
    <div className="offline-shell">
      <div className="offline-card">
        <RynLockup />
        <AlertTriangle size={28} />
        <h1>{tr("Cannot connect to this device")}</h1>
        <p>
          {tr("Ryn could not connect to its local service. Wait a moment and retry, or use Restart Node from the system tray.")}
        </p>
        <button type="button" onClick={() => void onRetry()}>
          <RotateCcw size={15} />
          {tr("Retry")}
        </button>
      </div>
      {isTauriDesktop() && <div className="pf-shell" style={{ display: "block", width: "100%", padding: 28 }}><DesktopPage /></div>}
    </div>
  );
}

export function AppRoutes() {
  useUILanguage();
  return (
    <UnlockGate>
      <Routes>
        <Route path="/" element={<App />}>
          <Route index element={<PersonalHome />} />
          <Route path="digest" element={<Digest />} />
          <Route path="explore" element={<Explore />} />
          <Route path="items/:contentId" element={<ItemDetail />} />
          <Route
            path="recommendations"
            element={<Navigate replace to="/digest" />}
          />
          <Route path="search-ask" element={<SearchAsk />} />
          <Route path="publish" element={<Publish />} />
          <Route path="peers" element={<Peers />} />
          <Route path="services" element={<PersonalServices />} />
          <Route path="services/catalog" element={<ServicesCatalog />} />
          <Route path="devices" element={<PersonalDevices />} />
          <Route path="tasks" element={<PersonalTasks />} />
          <Route path="services/manage" element={<Services />} />
          <Route path="services/sources" element={<AISources />} />
          <Route path="services/api" element={<APIAccessPage />} />
          <Route path="services/model-mapping" element={<ModelMappingPage />} />
          <Route path="services/agent-sharing" element={<AgentSharingPage />} />
          <Route path="services/private-ai/chat" element={<PrivateAIChat />} />
          <Route path="services/video-rendering" element={<VideoRendering />} />
          <Route
            path="services/secure-web-access"
            element={<SecureWebAccess />}
          />
          <Route path="chat" element={<Chat />} />
          <Route path="settings" element={<PersonalSettings />} />
          <Route path="nas" element={<Suspense fallback={<LoadingPanel />}><NasPage /></Suspense>} />
          <Route path="settings/plugins" element={<Suspense fallback={<LoadingPanel />}><PluginsPage /></Suspense>} />
          <Route path="settings/desktop" element={<DesktopPage />} />
          <Route path="settings/space" element={<PersonalSpacePage />} />
          <Route path="settings/advanced" element={<Settings />} />
        </Route>
      </Routes>
    </UnlockGate>
  );
}
