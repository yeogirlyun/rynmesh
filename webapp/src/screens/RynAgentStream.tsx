import { useEffect, useRef, useState } from "react";
import ChatMarkdown from "./ChatMarkdown";

const INITIAL_BUFFER_MS = 80;
const FRAME_MS = 32;
const CATCH_UP_MS = 180;

interface Props {
  text: string;
  streamKey: string;
  active?: boolean;
}

function useReducedMotion() {
  const [reduced, setReduced] = useState(() => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false);
  useEffect(() => {
    const media = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!media) return;
    const update = () => setReduced(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return reduced;
}

function StreamText({ text, active }: { text: string; active: boolean }) {
  const reducedMotion = useReducedMotion();
  const [visible, setVisible] = useState(() => active && !reducedMotion ? "" : text);
  const buffer = useRef({ text: visible, target: text, started: Boolean(visible), catchUpAt: 0, timer: null as ReturnType<typeof setTimeout> | null });

  useEffect(() => {
    const current = buffer.current;
    current.target = text;
    // Completion and corrected snapshots must be visible immediately. Only an
    // append-only live stream benefits from a small presentation buffer.
    if (!active || reducedMotion || !text.startsWith(current.text)) {
      if (current.timer !== null) clearTimeout(current.timer);
      current.timer = null;
      current.text = text;
      current.started = Boolean(text);
      setVisible(text);
      return;
    }
    if (current.text === text) {
      if (current.timer !== null) clearTimeout(current.timer);
      current.timer = null;
      return;
    }
    if (current.timer !== null) return;

    // Keep this deadline while new chunks arrive so a continuous stream cannot
    // continually push already received text farther into the future.
    current.catchUpAt = Date.now() + CATCH_UP_MS;
    const reveal = () => {
      current.timer = null;
      const remaining = Array.from(current.target.slice(current.text.length));
      if (!remaining.length) return;
      const frames = Math.max(1, Math.ceil((current.catchUpAt - Date.now()) / FRAME_MS));
      const count = Math.max(4, Math.ceil(remaining.length / frames));
      // Code points keep emoji and other surrogate pairs intact at every frame.
      current.text += remaining.slice(0, count).join("");
      current.started = true;
      setVisible(current.text);
      if (current.text !== current.target) current.timer = setTimeout(reveal, FRAME_MS);
    };
    current.timer = setTimeout(reveal, current.started ? FRAME_MS : INITIAL_BUFFER_MS);
  }, [text, active, reducedMotion]);

  useEffect(() => () => {
    if (buffer.current.timer !== null) clearTimeout(buffer.current.timer);
    buffer.current.timer = null;
  }, []);

  const shown = !active || reducedMotion || !text.startsWith(visible) ? text : visible;
  return <ChatMarkdown>{shown}</ChatMarkdown>;
}

/** Coalesce tiny first deltas and spread bursty updates over a few short frames. */
export default function RynAgentStream({ text, streamKey, active = true }: Props) {
  return <StreamText key={streamKey} text={text} active={active} />;
}
