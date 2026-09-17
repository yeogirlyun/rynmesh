import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { friendWeek, type FriendWeek } from "../domain/friendWeekly";
import { Button, Panel } from "./ui";

export default function FriendWeeklyRecap() {
  const [week, setWeek] = useState<FriendWeek | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true;
    let running = false;
    let controller: AbortController | undefined;
    const load = async () => {
      if (running) return;
      running = true; controller = new AbortController();
      const timeout = window.setTimeout(() => controller?.abort(), 10000);
      try {
        const result = await friendWeek(controller.signal);
        if (active) { setWeek(result); setError(""); }
      } catch {
        // A failed permission refresh must not leave private old rows visible.
        if (active) { setWeek(null); setError("Friend recap is unavailable. Reconnect to your node and retry."); }
      } finally { window.clearTimeout(timeout); running = false; }
    };
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => { active = false; controller?.abort(); window.clearInterval(timer); };
  }, [retry]);
  return <Panel><section aria-labelledby="friend-week-title">
    <h2 id="friend-week-title">Friends shared this week</h2>
    <p>A private local list from Monday 00:00 UTC through now. No friend content is sent to AI, the public briefing or recap email.</p>
    {error ? <p role="alert">{error}</p> : !week ? <p>Loading this week's friend updates…</p> : <>
      <p>Week starts {week.week_start.slice(0, 10)} · UTC · {week.items.length} of {week.available_count} received publications shown.</p>
      {week.incomplete ? <p>Some updates are unavailable or not loaded yet. This list may be incomplete.</p> : null}
      {!week.items.length ? <p>No publications received this week from friends you currently follow.</p> : <ul>{week.items.map((item) => <li key={`${item.peer_id}:${item.publication_id}`}>
        <strong>{item.title}</strong> · {item.node_name} · {new Date(item.published_at * 1000).toLocaleDateString("en", { timeZone: "UTC" })}
        {item.access_unconfirmed ? " · Latest access unconfirmed" : ""}
      </li>)}</ul>}
    </>}
    <p>Current access is checked when you save or open a publication. Offline changes may not have reached this node yet.</p>
    <Link to="/friend-updates">Open friend updates</Link>{" "}<Button onClick={() => setRetry((value) => value + 1)}>Refresh friend recap</Button>
  </section></Panel>;
}
