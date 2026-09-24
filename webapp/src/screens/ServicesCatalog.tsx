import { tr, useUILanguage, uiLocale } from "../uiI18n";
import {
  ArrowRight,
  Bot,
  Film,
  LockKeyhole,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ServiceArt } from "../personal/components";
import { personalHref } from "../personal/model";
import { useAppContext } from "../appContext";
import { LoadingPanel } from "../components/ui";
import type { JobCapacity } from "../domain/types";
import type { LLMServiceRecord } from "../domain/nodeClient";
import styles from "./ServicesCatalog.module.css";

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
  icon: typeof Bot;
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
  return capacities.find((capacity) => capacity.capabilities.some((item) => /video|veo|motion/i.test(item)));
}

export default function ServicesCatalog() {
  const uiLanguage = useUILanguage();
  const { client } = useAppContext();
  const navigate = useNavigate();
  const [llmServices, setLlmServices] = useState<LLMServiceRecord[]>([]);
  const [capacities, setCapacities] = useState<JobCapacity[]>([]);
  const [networkId, setNetworkId] = useState("rynmesh-main");
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<ServiceCategory>("all");
  const [recent, setRecent] = useState<RecentService[]>(loadRecentServices);

  useEffect(() => {
    let active = true;
    void (async () => {
      const settings = await client.getSettings().catch(() => null);
      const network = settings?.network_id?.trim() || "rynmesh-main";
      const [llmResult, capacityResult] = await Promise.allSettled([
        client.listLLMServices(network),
        client.listJobCapacities(),
      ]);
      if (!active) return;
      setNetworkId(network);
      if (llmResult.status === "fulfilled") setLlmServices(llmResult.value);
      if (capacityResult.status === "fulfilled") setCapacities(capacityResult.value);
      setLoading(false);
    })();
    return () => { active = false; };
  }, [client]);

  const services = useMemo<CatalogService[]>(() => {
    const llm = llmServices.find((service) => service.online) ?? llmServices[0];
    const video = findVideoCapacity(capacities);
    const llmPrice = llm?.service.pricing.minimum ?? 0;
    const llmCurrency = llm?.service.pricing.currency === "DEV_TASK_BALANCE" ? "credits" : llm?.service.pricing.currency;
    const videoCapability = video?.capabilities.find((item) => /video|veo|motion/i.test(item));
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
        available: Boolean(llm?.online),
        icon: Bot,
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
        icon: Film,
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
        icon: ShieldCheck,
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

  return (
    <div className={styles.page}>
      <header className={styles.hero}>
        <div>

          <h1>{tr("Browse services")}</h1>
          <p>{tr("Tools available across your connected devices.")}</p>
        </div>
        <div className={styles.heroTools}>
          <label className={styles.search}>
            <Search size={17} />
            <input
              aria-label={tr("Search services")}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={tr("Search services")}
            />
          </label>
          <button className={styles.manageButton} type="button" onClick={() => navigate(personalHref("/services/manage", client.mode === "fixture"))}>
            <Settings2 size={16} /> {tr("Manage")}
          </button>
        </div>
      </header>

      <div className={styles.filters} aria-label={tr("Service categories")}>
        {filters.map((item) => (
          <button
            key={item.id}
            type="button"
            className={`${styles.filter}${filter === item.id ? ` ${styles.filterActive}` : ""}`}
            aria-pressed={filter === item.id}
            onClick={() => setFilter(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <section>
        <div className={styles.sectionHeading}>
          <h2>{tr("Available now")}</h2>
          <span>{tr("{{count}} services", { count: visible.length })}</span>
        </div>
        <div className={styles.grid}>
          {visible.map((service) => {

            return (
              <article className={styles.card} key={service.id}>
                <div className={styles.cardTop}>
                  <ServiceArt kind={service.id === "private-ai" ? "ai" : service.id === "video-rendering" ? "video" : "network"} />
                  <div className={styles.cardTitle}>
                    <span className={styles.type}>{tr(service.experience)}</span>
                    <h3>{service.title}</h3>
                  </div>
                </div>
                <p className={styles.description}>{service.description}</p>
                <div className={styles.meta}>
                  <span className={service.available ? styles.ready : styles.unavailable}>{service.available ? tr("Ready") : tr("Unavailable")}</span>
                  <span>{service.price}</span>
                </div>
                <button className={styles.cardAction} disabled={!service.available && service.id !== "private-ai"} type="button" onClick={() => openService(service)}>
                  {service.action} <ArrowRight size={16} />
                </button>
              </article>
            );
          })}
          {!visible.length ? (
            <div className={styles.empty}>
              <Sparkles size={24} />
              <h3>{tr("No matching services")}</h3>
              <p>{tr("Try a different search or category.")}</p>
            </div>
          ) : null}
        </div>
      </section>

      {recentServices.length ? (
        <section className={styles.recentSection} aria-label={tr("Recently used services")}>
          <div className={styles.sectionHeading}>
            <h2>{tr("Recently used")}</h2>
          </div>
          <div className={styles.recentList}>
            {recentServices.map(({ item, service }) => {
              const Icon = service.icon;
              return (
                <button type="button" key={service.id} className={styles.recentRow} onClick={() => openService(service)}>
                  <span className={styles.recentIcon}><Icon size={17} /></span>
                  <span className={styles.recentTitle}>{service.title}</span>
                  <span className={styles.recentTime}>{new Date(item.openedAt).toLocaleDateString(uiLocale(), { month: "short", day: "numeric" })}</span>
                  <ArrowRight size={15} />
                </button>
              );
            })}
          </div>
        </section>
      ) : null}

      <footer className={styles.footer}>
        <LockKeyhole size={14} /> {tr("Providers and routes are selected automatically")}
      </footer>
    </div>
  );
}
