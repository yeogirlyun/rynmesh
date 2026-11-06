import { createContext, useCallback, useContext, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { ExtensionPlugin } from "../domain/agentExtensions";
import type { NodeClient } from "../domain/nodeClient";
import { PluginWebApp } from "./ExtensionWebApp";

/**
 * Plugin pages opened in this window stay loaded after the user switches to
 * another page, so coming back shows them as they were instead of starting the
 * plugin again. The Gshopper pages are three plugins, hence three.
 */
export const KEPT_PLUGIN_PAGES = 3;

interface KeptPage { id: string; plugin: ExtensionPlugin }
interface Keeper {
  pages: KeptPage[];
  active: string | null;
  show: (plugin: ExtensionPlugin) => void;
  hide: (id: string) => void;
  drop: (id: string) => void;
}

const KeeperContext = createContext<Keeper | null>(null);

/** Same page for the user: a refreshed manifest must not reload a kept page. */
function samePage(left: ExtensionPlugin, right: ExtensionPlugin) {
  return left.enabled === right.enabled && left.title === right.title &&
    JSON.stringify(left.web_app ?? null) === JSON.stringify(right.web_app ?? null);
}

export function KeptPluginPagesProvider({ children }: { children: ReactNode }) {
  const [pages, setPages] = useState<KeptPage[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const show = useCallback((plugin: ExtensionPlugin) => {
    setPages(current => {
      const known = current.find(page => page.id === plugin.id);
      const kept = known && samePage(known.plugin, plugin) ? known : { id: plugin.id, plugin };
      // Most recently shown last; the oldest page is closed first.
      return [...current.filter(page => page.id !== plugin.id), kept].slice(-KEPT_PLUGIN_PAGES);
    });
    setActive(plugin.id);
  }, []);
  const hide = useCallback((id: string) => setActive(current => current === id ? null : current), []);
  const drop = useCallback((id: string) => {
    setPages(current => current.some(page => page.id === id) ? current.filter(page => page.id !== id) : current);
    setActive(current => current === id ? null : current);
  }, []);
  const value = useMemo(() => ({ pages, active, show, hide, drop }), [pages, active, show, hide, drop]);
  return <KeeperContext.Provider value={value}>{children}</KeeperContext.Provider>;
}

export function useKeptPluginPages() {
  return useContext(KeeperContext);
}

/** Route placeholder: shows the kept page for this plugin while the route is open. */
export function KeptPluginPage({ plugin }: { plugin: ExtensionPlugin }) {
  const keeper = useKeptPluginPages()!;
  const { show, hide } = keeper;
  useLayoutEffect(() => { show(plugin); }, [plugin, show]);
  useLayoutEffect(() => () => hide(plugin.id), [plugin.id, hide]);
  return null;
}

/**
 * Renders every kept page in the content area. Hidden pages keep the size they
 * were shown at, so returning neither reloads nor re-lays out the plugin.
 */
export function KeptPluginPages({ client }: { client: NodeClient }) {
  const keeper = useKeptPluginPages();
  const container = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const active = keeper?.active ?? null;
  useLayoutEffect(() => {
    const element = container.current;
    if (!active || !element) return;
    const measure = () => {
      const { width, height } = element.getBoundingClientRect();
      if (width > 0 && height > 0) setSize(current => current?.width === width && current.height === height ? current : { width, height });
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [active]);
  if (!keeper || keeper.pages.length === 0) return null;
  return <div ref={container} className={`pf-kept-plugins${active ? " is-active" : ""}`}
    style={active || !size ? undefined : { width: size.width, height: size.height }}>
    {keeper.pages.map(page => {
      const shown = page.id === active;
      return <div key={page.id} className={`pf-kept-plugin${shown ? " is-active" : ""}`} data-plugin-id={page.id}
        aria-hidden={shown ? undefined : true} inert={!shown}>
        <PluginWebApp pluginId={page.id} resolvedPlugin={page.plugin} client={client} visible={shown} />
      </div>;
    })}
  </div>;
}
