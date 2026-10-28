import { tr, useUILanguage, uiLocale } from "../uiI18n";
import {
  ArrowRight,
  Clock,
  LockKeyhole,
  Search,
  Settings2,
  TriangleAlert,
} from "lucide-react";
import { useMemo, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { personalHref } from "../personal/model";
import { useAppContext } from "../appContext";
import { LoadingPanel } from "../components/ui";
import type { JobCapacity } from "../domain/types";
import type { LLMServiceRecord } from "../domain/nodeClient";
import { isServiceUsable } from "../domain/llmOrders";
import { useProviderDiscovery, useServiceOrder } from "../domain/serviceExperience";
import { providerIdentity, serviceDescriptors } from "../domain/serviceDescriptors";
import { ChatGlyph, GlyphTile, ShieldGlyph, VideoGlyph, type ServiceHue } from "./ServiceParts";
import "./service-pages-refresh.css";

type ServiceCategory = "all" | "ai" | "creative" | "network";

interface CatalogService {
  id: string;
  title: string;
  description: string;
  category: Exclude<ServiceCategory, "all">;
  experience: "Chat" | "Workflow" | "Connection";
  price: string;
  action: string;
  available: boolean;
  hue: ServiceHue;
  glyph: (size: number) => ReactNode;
  href: string;
}

interface RecentService {
  id: string;
  openedAt: number;
}

const RECENT_STORAGE_KEY = "ryn.services.recent.v1";

function loadRecentServices(): RecentService[] {
  // This preference contains only public service IDs and timestamps. Prompt,
  // provider, route, and result data belong to their dedicated storage paths.
  try {
    const stored = JSON.parse(window.localStorage.getItem(RECENT_STORAGE_KEY) ?? "[]") as RecentService[];
    return Array.isArray(stored) ? stored.filter((item) => item && typeof item.id === "string" && typeof item.openedAt === "number") : [];
  } catch {
    return [];
  }
}

const filters: Array<{ id: ServiceCategory; label: string }> = [
  { id: "all", get label() { return tr("All"); } },
  { id: "ai", label: "AI" },
  { id: "creative", get label() { return tr("Creative"); } },
  { id: "network", get label() { return tr("Network"); } },
];

function llmHref(service: LLMServiceRecord, networkId: string, clientMode: "live" | "fixture") {
  const query = new URLSearchParams({
    peer: service.peer_id,
    service: service.service.package_id,
    network: networkId,
  });
  if (clientMode === "fixture") query.set("client", "fixture");
  return `/services/private-ai/chat?${query.toString()}`;
}

function findVideoCapacity(capacities: JobCapacity[]) {
  return capacities.find((capacity) => capacity.capabilities.includes(serviceDescriptors.video.capability));
}

export default function ServicesCatalog() {
  const uiLanguage = useUILanguage();
  const { client } = useAppContext();
  const navigate = useNavigate();
  const network = useServiceOrder({ scope: client, key: "service-network", intervalMs: 15_000,
    load: async () => (await client.getSettings().catch(() => null))?.network_id?.trim() || "rynmesh-main",
    isTerminal: () => true,
  });
  const networkId = network.data ?? "rynmesh-main";
  const llm = useProviderDiscovery<LLMServiceRecord>({ scope: client, key: networkId,
    enabled: Boolean(network.data), intervalMs: serviceDescriptors.privateAI.discoveryIntervalMs,
    load: () => client.listLLMServices(networkId),
    identity: (item) => providerIdentity(networkId, item.peer_id, item.service.package_id),
  });
  const video = useProviderDiscovery<JobCapacity>({ scope: client, key: serviceDescriptors.video.id,
    intervalMs: serviceDescriptors.video.discoveryIntervalMs, load: () => client.listJobCapacities(),
    identity: (item) => providerIdentity(item.network_id, item.peer_id, serviceDescriptors.video.capability),
  });
  const llmServices = llm.providers;
  const capacities = video.providers;
  const loading = network.loading || llm.loading || video.loading;
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<ServiceCategory>("all");
  const [recent, setRecent] = useState<RecentService[]>(loadRecentServices);

  const services = useMemo<CatalogService[]>(() => {
    const llm = llmServices.find(isServiceUsable) ?? llmServices[0];
    const video = findVideoCapacity(capacities);
    const llmPrice = llm?.service.pricing.minimum ?? 0;
    const llmCurrency = llm?.service.pricing.currency === "DEV_TASK_BALANCE" ? "credits" : llm?.service.pricing.currency;
    const videoCapability = video ? serviceDescriptors.video.capability : undefined;
    const videoPrice = videoCapability ? Number(video?.price_credits[videoCapability] ?? 0) : 0;
    return [
      {
        id: "private-ai",
        title: tr("AI chat"),
        description: tr("Talk to an AI model running on a connected device."),
        category: "ai",
        experience: "Chat",
        price: llm ? tr("From {{v0}} {{v1}}", { v0: llmPrice, v1: tr(llmCurrency || "credits") }) : tr("No provider online"),
        action: tr("Open chat"),
        available: Boolean(llm && isServiceUsable(llm)),
        hue: "indigo",
        glyph: (size) => <ChatGlyph size={size} />,
        href: llm ? llmHref(llm, networkId, client.mode) : "/services/manage#private-ai",
      },
      {
        id: "video-rendering",
        title: tr("Video rendering"),
        description: tr("Create and render video clips with an available provider."),
        category: "creative",
        experience: "Workflow",
        price: video ? tr("{{count}} credits", { count: videoPrice }) : tr("No provider online"),
        action: tr("Create video"),
        available: Boolean(video),
        hue: "rose",
        glyph: (size) => <VideoGlyph size={size} />,
        href: `/services/video-rendering${client.mode === "fixture" ? "?client=fixture" : ""}`,
      },
      {
        id: "secure-web-access",
        title: tr("Secure web access"),
        description: tr("Browse through a trusted encrypted route."),
        category: "network",
        experience: "Connection",
        price: tr("Price shown before connecting"),
        action: tr("Connect"),
        available: true,
        hue: "teal",
        glyph: (size) => <ShieldGlyph size={size} />,
        href: `/services/secure-web-access${client.mode === "fixture" ? "?client=fixture" : ""}`,
      },
    ];
  }, [capacities, client.mode, llmServices, networkId, uiLanguage]);

  const visible = services.filter((service) => {
    const matchesCategory = filter === "all" || service.category === filter;
    const needle = query.trim().toLowerCase();
    const matchesQuery = !needle || `${service.title} ${service.description} ${tr(service.experience)}`.toLowerCase().includes(needle);
    return matchesCategory && matchesQuery;
  });

  const openService = (service: CatalogService) => {
    if (!service.available && service.id !== "private-ai") return;
    const next = [{ id: service.id, openedAt: Date.now() }, ...recent.filter((item) => item.id !== service.id)].slice(0, 3);
    setRecent(next);
    try {
      window.localStorage.setItem(RECENT_STORAGE_KEY, JSON.stringify(next));
    } catch {
      // Recent service shortcuts are optional and never contain private content.
    }
    navigate(personalHref(service.href, client.mode === "fixture"));
  };

  const recentServices = recent
    .map((item) => ({ item, service: services.find((service) => service.id === item.id) }))
    .filter((entry): entry is { item: RecentService; service: CatalogService } => Boolean(entry.service));

  if (loading) return <LoadingPanel label={tr("Finding available services")} />;

  const counts: Record<ServiceCategory, number> = {
    all: services.length,
    ai: services.filter((service) => service.category === "ai").length,
    creative: services.filter((service) => service.category === "creative").length,
    network: services.filter((service) => service.category === "network").length,
  };

  return (
    <div className="rf rd-svc rd-catalog">
      <header className="sv-head">
        <div className="sv-head-text">
          <h1 className="sv-title">{tr("Browse services")}</h1>
          <p className="sv-lead">{tr("Tools available across your connected devices.")}</p>
        </div>
        <div className="sv-head-actions">
          <label className="sv-search">
            <Search size={15} aria-hidden="true" />
            <input
              aria-label={tr("Search services")}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={tr("Search services")}
            />
          </label>
          <button className="rf-btn" type="button" onClick={() => navigate(personalHref("/services/manage", client.mode === "fixture"))}>
            <Settings2 size={15} aria-hidden="true" /> {tr("Manage")}
          </button>
        </div>
      </header>

      {llm.error || video.error ? (
        <div className="sv-alert" role="alert">
          <TriangleAlert size={18} aria-hidden="true" />
          <p>{tr("Some services could not be refreshed. Previously discovered services may be out of date.")}</p>
          <button type="button" className="rf-btn rf-btn-sm" onClick={() => { void llm.refresh(); void video.refresh(); }}>{tr("Retry discovery")}</button>
        </div>
      ) : null}

      <div className="sv-filters">
        <div className="rf-seg" role="group" aria-label={tr("Service categories")}>
          {filters.map((item) => (
            <button key={item.id} type="button" aria-pressed={filter === item.id} onClick={() => setFilter(item.id)}>
              {item.label}<em aria-hidden="true">{counts[item.id]}</em>
            </button>
          ))}
        </div>
        <span className="sv-count">{tr("{{count}} services", { count: visible.length })}</span>
      </div>

      <section className="sv-sec" aria-labelledby="sv-available">
        <h2 id="sv-available">{tr("Available now")}</h2>
        <div className="sv-grid">
          {visible.map((service) => (
            <article className={`sv-svc rf-h-${service.hue}`} key={service.id}>
              <div className="sv-svc-top">
                <GlyphTile hue={service.hue} size="lg">{service.glyph}</GlyphTile>
                <div>
                  <span className="sv-svc-type">{tr(service.experience)}</span>
                  <h3>{service.title}</h3>
                </div>
              </div>
              <p className="sv-svc-desc">{service.description}</p>
              <div className="sv-svc-meta">
                <span className={`rf-pill rf-pill-sm rf-t-${service.available ? "ok" : "off"}`}><i aria-hidden="true" />{service.available ? tr("Ready") : tr("Unavailable")}</span>
                <span>{service.price}</span>
              </div>
              <button className={`rf-btn rf-btn-block${service.available ? " rf-btn-p" : ""}`} disabled={!service.available && service.id !== "private-ai"} type="button" onClick={() => openService(service)}>
                {service.action} <ArrowRight size={15} aria-hidden="true" />
              </button>
            </article>
          ))}
          {!visible.length ? (
            <div className="sv-empty">
              <Search size={28} strokeWidth={1.5} aria-hidden="true" />
              <h3>{tr("No matching services")}</h3>
              <p>{tr("Try a different search or category.")}</p>
            </div>
          ) : null}
        </div>
      </section>

      {recentServices.length ? (
        <section className="sv-sec" aria-label={tr("Recently used services")}>
          <h2>{tr("Recently used")}</h2>
          <div className="sv-recent">
            {recentServices.map(({ item, service }) => (
              <button type="button" key={service.id} className={`sv-recent-row rf-h-${service.hue}`} onClick={() => openService(service)}>
                <span className="rf-chipicon" aria-hidden="true"><Clock size={16} /></span>
                <b>{service.title}</b>
                <small>{new Date(item.openedAt).toLocaleDateString(uiLocale(), { month: "short", day: "numeric" })}</small>
                <ArrowRight size={15} aria-hidden="true" />
              </button>
            ))}
          </div>
        </section>
      ) : null}

      <p className="sv-foot">
        <LockKeyhole size={13} aria-hidden="true" /> {tr("Providers and routes are selected automatically")}
      </p>
    </div>
  );
}
