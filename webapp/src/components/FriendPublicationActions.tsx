import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { askHistory } from "../domain/askHistory";
import { feedApi } from "../domain/friendFeed";
import { offlineApi, offlineLabels } from "../domain/offlineReading";
import { Button } from "./ui";

export type PublicationReference = { relationship_id: string; publication_id: string; revision: number };

export default function FriendPublicationActions({ publication }: { publication: PublicationReference }) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const saved = useRef("");
  const pending = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const run = async (action: "offline" | "ask") => {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError(""); setNotice("");
    try {
      if (!saved.current) {
        const copy = await feedApi.fetch(publication.relationship_id, publication.publication_id, publication.revision);
        saved.current = copy.library_id;
      }
      if (!mounted.current) return;
      if (action === "offline") {
        const row = await offlineApi.download(saved.current);
        if (mounted.current) setNotice(`${offlineLabels[row.state] ?? "Check download status"}. The verified source copy is also saved in My content.`);
      } else {
        const source = await askHistory.prepareContext(saved.current);
        if (mounted.current) navigate(`/ask?material=${encodeURIComponent(source.library_id)}`);
      }
    } catch (cause) {
      if (mounted.current) setError(`${saved.current ? "Your source copy is saved in My content. " : ""}${cause instanceof Error ? cause.message : "Could not confirm this action. Retry when connected."}`);
    } finally { pending.current = false; if (mounted.current) setBusy(false); }
  };
  return <div>
    <Button disabled={busy} onClick={() => void run("offline")}>Save offline</Button>{" "}
    <Button disabled={busy} onClick={() => void run("ask")}>Ask about this</Button>
    <p>Both actions save a private copy after checking access. Asking opens material review before anything is sent to AI. Previously saved copies remain after sharing stops.</p>
    {notice ? <p role="status">{notice} <Link to="/offline">View offline downloads</Link></p> : null}
    {error ? <p role="alert">{error}</p> : null}
  </div>;
}

// This projection is optional: public digest items have no friend provenance.
// It also lets the action join the separately reviewed For You projection.
export function DigestFriendActions({ item }: { item: object }) {
  const publication = (item as { friend_provenance?: PublicationReference }).friend_provenance;
  return publication ? <FriendPublicationActions key={`${publication.relationship_id}:${publication.publication_id}:${publication.revision}`} publication={publication} /> : null;
}
