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

const filters: Array<{ id: ServiceCategory; label: string }> = [
  { id: "all", label: "All" },
  { id: "ai", label: "AI" },
  { id: "creative", label: "Creative" },
  { id: "network", label: "Network" },
];

function llmHref(service: LLMServiceRecord, networkId: string) {
  const query = new URLSearchParams({
    peer: service.peer_id,
    service: service.service.package_id,
    network: networkId,
  });
  return `/services/private-ai/chat?${query.toString()}`;
}

function findVideoCapacity(capacities: JobCapacity[]) {
  return capacities.find((capacity) => capacity.capabilities.some((item) => /video|veo|motion/i.test(item)));
}

export default function ServicesCatalog() {
  const { client } = useAppContext();
  const navigate = useNavigate();
  const [llmServices, setLlmServices] = useState<LLMServiceRecord[]>([]);
  const [capacities, setCapacities] = useState<JobCapacity[]>([]);
  const [networkId, setNetworkId] = useState("rynmesh-main");
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<ServiceCategory>("all");

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
        title: "Private AI",
        description: "Have a private conversation with a language model.",
        category: "ai",
        experience: "Chat",
        price: llm ? `From ${llmPrice} ${llmCurrency || "credits"}` : "No provider online",
        action: "Open chat",
        available: Boolean(llm?.online),
        icon: Bot,
        href: llm ? llmHref(llm, networkId) : "/services/manage#private-ai",
      },
      {
        id: "video-rendering",
        title: "Video rendering",
        description: "Create and render video clips with an available provider.",
        category: "creative",
        experience: "Workflow",
        price: video ? `${videoPrice} credits` : "No provider online",
        action: "Create video",
        available: Boolean(video),
        icon: Film,
        href: "/services/manage#video-rendering",
      },
      {
        id: "secure-web-access",
        title: "Secure web access",
        description: "Browse through a trusted encrypted route.",
        category: "network",
        experience: "Connection",
        price: "Price shown before connecting",
        action: "Connect",
        available: true,
        icon: ShieldCheck,
        href: "/settings#web-access",
      },
    ];
  }, [capacities, llmServices, networkId]);

  const visible = services.filter((service) => {
    const matchesCategory = filter === "all" || service.category === filter;
    const needle = query.trim().toLowerCase();
    const matchesQuery = !needle || `${service.title} ${service.description} ${service.experience}`.toLowerCase().includes(needle);
    return matchesCategory && matchesQuery;
  });

  const openService = (service: CatalogService) => {
    if (!service.available && service.id !== "private-ai") return;
    navigate(service.href);
  };

  if (loading) return <LoadingPanel label="Finding available services" />;

  return (
    <div className={styles.page}>
      <header className={styles.hero}>
        <div>
          <span className="eyebrow">Ryn services</span>
          <h1>Choose a service</h1>
          <p>Each service opens the experience designed for it.</p>
        </div>
        <div className={styles.heroTools}>
          <label className={styles.search}>
            <Search size={17} />
            <input
              aria-label="Search services"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search services"
            />
          </label>
          <button className={styles.manageButton} type="button" onClick={() => navigate("/services/manage")}>
            <Settings2 size={16} /> Manage
          </button>
        </div>
      </header>

      <div className={styles.filters} aria-label="Service categories">
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
          <h2>Available now</h2>
          <span>{visible.length} {visible.length === 1 ? "service" : "services"}</span>
        </div>
        <div className={styles.grid}>
          {visible.map((service) => {
            const Icon = service.icon;
            const iconClass = service.category === "creative" ? styles.iconCreative : service.category === "network" ? styles.iconNetwork : "";
            return (
              <article className={styles.card} key={service.id}>
                <div className={styles.cardTop}>
                  <span className={`${styles.icon}${iconClass ? ` ${iconClass}` : ""}`}><Icon size={28} /></span>
                  <div className={styles.cardTitle}>
                    <span className={styles.type}>{service.experience}</span>
                    <h3>{service.title}</h3>
                  </div>
                </div>
                <p className={styles.description}>{service.description}</p>
                <div className={styles.meta}>
                  <span className={styles.ready}>{service.available ? "Ready" : "Unavailable"}</span>
                  <span>{service.price}</span>
                </div>
                <button className={styles.cardAction} type="button" onClick={() => openService(service)}>
                  {service.action} <ArrowRight size={16} />
                </button>
              </article>
            );
          })}
          {!visible.length ? (
            <div className={styles.empty}>
              <Sparkles size={24} />
              <h3>No matching services</h3>
              <p>Try a different search or category.</p>
            </div>
          ) : null}
        </div>
      </section>

      <footer className={styles.footer}>
        <LockKeyhole size={14} /> Providers and routes are selected automatically
      </footer>
    </div>
  );
}

