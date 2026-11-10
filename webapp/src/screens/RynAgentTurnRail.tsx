import { useEffect, useState, type RefObject } from "react";
import { useTranslation } from "react-i18next";
import "./RynAgentTurnRail.css";

export interface TurnRailItem {
  turnIndex: number;
  preview: string;
}

export default function RynAgentTurnRail({
  turns,
  scrollRef,
}: {
  turns: TurnRailItem[];
  scrollRef: RefObject<HTMLDivElement | null>;
}) {
  const { i18n } = useTranslation("rynAgent");
  const zh = i18n.language.startsWith("zh");
  const [activeTurn, setActiveTurn] = useState(() => Math.max(0, turns.length - 1));
  const [hoveredTurn, setHoveredTurn] = useState<number | null>(null);

  const [overflowing, setOverflowing] = useState(false);
  const turnKey = turns.map(item => item.turnIndex).join(",");

  useEffect(() => {
    setHoveredTurn(null);
    if (turns.length < 2) {
      setOverflowing(false);
      return;
    }
    const container = scrollRef.current;
    if (!container) return;
    const measure = () => setOverflowing(container.scrollHeight > container.clientHeight + 24);
    measure();
    const resize = typeof ResizeObserver !== "undefined" ? new ResizeObserver(measure) : null;
      resize?. observe(container);
    const observeContent = () => {
      resize?.disconnect();
        resize?. observe(container);
      for (const child of container.children) resize?.observe(child);
      measure();
    };
    observeContent();
    const mutations = new MutationObserver(observeContent);
    mutations.observe(container, { childList: true, subtree: true, characterData: true });
    return () => { resize?.disconnect(); mutations.disconnect(); };
  }, [scrollRef, turnKey]);

  useEffect(() => {
    const container = scrollRef.current;
    if (!container || turns.length < 2) return;
    const onScroll = () => {
      const nodes = Array.from(container.querySelectorAll<HTMLElement>("[data-turn-user]"));
      if (!nodes.length) return;
      const containerRect = container.getBoundingClientRect();
      const threshold = containerRect.top + containerRect.height * 0.42;
      let chosen = Number(nodes[0].dataset.turnUser) || 0;
      for (const node of nodes) {
        const rect = node.getBoundingClientRect();
        if (rect.top <= threshold) {
          chosen = Number(node.dataset.turnUser) || 0;
        } else {
          break;
        }
      }
      setActiveTurn(chosen);
    };
    onScroll();
    container.addEventListener("scroll", onScroll, { passive: true });
    return () => container.removeEventListener("scroll", onScroll);
  }, [scrollRef, turnKey]);

  if (turns.length < 2 || !overflowing) return null;

  const jumpToTurn = (turnIndex: number) => {
    const container = scrollRef.current;
    if (!container) return;
    const target = container.querySelector<HTMLElement>(`[data-turn-user="${turnIndex}"]`);
    if (!target) return;
    setActiveTurn(turnIndex);
    target.scrollIntoView?.({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "start" });
    target.classList.remove("ra-turn-highlight");
    void target.offsetWidth;
    target.classList.add("ra-turn-highlight");
  };

  const hoveredItem = hoveredTurn !== null ? turns.find(item => item.turnIndex === hoveredTurn) : undefined;

  return (
    <div
      className="ra-turn-rail"
      role="navigation"
      aria-label={zh ? "对话导航" : "Conversation navigation"}
      onMouseLeave={() => setHoveredTurn(null)}
    >
      <div className="ra-turn-rail-ticks">
        {turns.map(item => {
          const isCurrent = item.turnIndex === activeTurn;
          return (
            <button
              type="button"
              key={item.turnIndex}
              aria-label={item.preview || (zh ? "查看消息" : "View message")}
              aria-current={isCurrent ? "location" : undefined}
              className={`ra-turn-rail-tick${isCurrent ? " is-active" : ""}`}
              onFocus={() => setHoveredTurn(item.turnIndex)}
              onBlur={() => setHoveredTurn(null)}
              onMouseEnter={() => setHoveredTurn(item.turnIndex)}
              onClick={() => jumpToTurn(item.turnIndex)}
            >
              <span className="ra-turn-rail-line" />
            </button>
          );
        })}
      </div>
      {hoveredItem && (
        <div
          className="ra-turn-rail-tooltip"
          onClick={() => jumpToTurn(hoveredItem.turnIndex)}
        >
          <p className="ra-turn-rail-tooltip-text">{hoveredItem.preview}</p>
        </div>
      )}
    </div>
  );
}
