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

  useEffect(() => {
    setActiveTurn(Math.max(0, turns.length - 1));
  }, [turns.length]);

  useEffect(() => {
    const container = scrollRef.current;
    if (!container || !turns.length) return;
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
  }, [scrollRef, turns.length]);

  if (!turns.length) return null;

  const jumpToTurn = (turnIndex: number) => {
    const container = scrollRef.current;
    if (!container) return;
    const target = container.querySelector<HTMLElement>(`[data-turn-user="${turnIndex}"]`);
    if (!target) return;
    setActiveTurn(turnIndex);
    target.scrollIntoView?.({ behavior: "smooth", block: "center" });
    target.classList.remove("ra-turn-highlight");
    void target.offsetWidth;
    target.classList.add("ra-turn-highlight");
  };

  const hoveredItem = hoveredTurn !== null ? turns.find(item => item.turnIndex === hoveredTurn) : undefined;

  return (
    <div
      className="ra-turn-rail"
      aria-hidden="true"
      onMouseLeave={() => setHoveredTurn(null)}
    >
      <div className="ra-turn-rail-ticks">
        {turns.map((item, idx) => {
          const isCurrent = item.turnIndex === activeTurn;
          const dist = hoveredTurn !== null ? Math.abs(idx - turns.findIndex(t => t.turnIndex === hoveredTurn)) : 99;
          const waveClass =
            dist === 0 ? " is-wave-0" : dist === 1 ? " is-wave-1" : dist === 2 ? " is-wave-2" : "";
          return (
            <div
              key={item.turnIndex}
              className={`ra-turn-rail-tick${isCurrent ? " is-active" : ""}${waveClass}`}
              onMouseEnter={() => setHoveredTurn(item.turnIndex)}
              onClick={() => jumpToTurn(item.turnIndex)}
            >
              <span className="ra-turn-rail-line" />
            </div>
          );
        })}
      </div>
      {hoveredItem && (
        <div
          className="ra-turn-rail-tooltip"
          onClick={() => jumpToTurn(hoveredItem.turnIndex)}
        >
          <div className="ra-turn-rail-tooltip-head">
            <span>
              {zh
                ? `第 ${hoveredItem.turnIndex + 1} / ${turns.length} 轮`
                : `Turn ${hoveredItem.turnIndex + 1} of ${turns.length}`}
            </span>
          </div>
          <p className="ra-turn-rail-tooltip-text">{hoveredItem.preview}</p>
        </div>
      )}
    </div>
  );
}
