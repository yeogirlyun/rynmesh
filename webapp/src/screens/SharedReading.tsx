import { useCallback, useEffect, useRef, useState } from "react";
import { useAppContext } from "../appContext";
import { Button, PageHeader, Panel } from "../components/ui";
import { friendsApi } from "../domain/friendsClient";
import type { FriendRecord } from "../domain/friendTypes";
import { sharedReading, type SharedList } from "../domain/sharedReading";

const newId = () => crypto.randomUUID().replaceAll("-", "");

function ListCard({ list, busy, run }: { list: SharedList; busy: boolean; run: (value: object) => Promise<boolean> }) {
  const { confirm } = useAppContext();
  const [title, setTitle] = useState("");
  const [url, setUrl] = useState("");
  const attempt = useRef({ key: "", id: "" });
  const disabled = busy || list.blocked || list.status !== "active";
  const edit = async (action: string, value: object) => {
    const key = JSON.stringify([list.id, action, value]);
    if (attempt.current.key !== key) attempt.current = { key, id: newId() };
    const result = await run({ action: "edit", id: list.id, operation_id: attempt.current.id, edit: action,
      value: action === "add" ? { ...value, id: attempt.current.id } : value });
    if (result) { attempt.current = { key: "", id: "" }; if (action === "add") { setTitle(""); setUrl(""); } }
  };
  return <Panel><article aria-label={`Shared list ${list.title}`}>
    <h2>{list.title}</h2><p>With {list.friend_name} · {list.status === "invited" ? "Waiting for your friend to accept" : list.status === "closed" ? "List closed" : list.blocked ? "Friendship inactive" : "Shared list active"}</p>
    <p>{list.pending_count} changes awaiting the creating node's acknowledgement. Displayed items are the last acknowledged version.</p>
    {list.error ? <p role="status">{list.error}</p> : null}
    <Button disabled={busy || list.blocked} onClick={() => void run({ action: "sync", id: list.id })}>Sync list</Button>
    <ul>{Object.values(list.items).filter((item) => !item.removed).map((item) => <li key={item.id}>
      <a href={item.url} target="_blank" rel="noreferrer">{item.title}</a>
      <p>You: {item.read_by[list.local_peer] ? "Read" : "Unread"} · Friend: {item.read_by[list.owner === list.local_peer ? list.friend : list.owner] ? "Read" : "Unread"}</p>
      <Button disabled={disabled} onClick={() => void edit("read", { id: item.id, read: !item.read_by[list.local_peer] })}>{item.read_by[list.local_peer] ? "Mark unread" : "Mark read"}</Button>{" "}
      <Button disabled={disabled} onClick={() => confirm({ title: "Remove this shared item?", body: "This removes the item from the shared list for both members. Saved articles and private reading history remain.", risk: "medium", confirmLabel: "Remove shared item", onConfirm: () => edit("remove", { id: item.id }) })}>Remove item</Button>
    </li>)}</ul>
    <label>Item title<input value={title} onChange={(event) => setTitle(event.target.value)} disabled={disabled} maxLength={500} /></label>
    <label>Item link<input value={url} onChange={(event) => setUrl(event.target.value)} disabled={disabled} placeholder="https://" maxLength={2048} /></label>
    <Button disabled={disabled || !title.trim() || !/^https?:\/\//.test(url)} onClick={() => void edit("add", { title, url })}>Add to shared list</Button>{" "}
    <Button disabled={disabled} onClick={() => confirm({ title: "Close this shared list?", body: "Either member can stop future collaboration. The other device learns on reconnect; previously saved copies cannot be recalled.", risk: "medium", confirmLabel: "Close shared list", onConfirm: () => edit("close", {}) })}>Close list</Button>
  </article></Panel>;
}

export default function SharedReading() {
  const { confirm } = useAppContext();
  const [lists, setLists] = useState<SharedList[]>([]);
  const [friends, setFriends] = useState<FriendRecord[]>([]);
  const [rid, setRid] = useState("");
  const [title, setTitle] = useState("");
  const [invites, setInvites] = useState<{ id: string; title: string; rid: string }[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [fresh, setFresh] = useState(false);
  const createId = useRef(newId());
  const mounted = useRef(true);
  const sequence = useRef(0);
  const load = useCallback(async () => {
    const ticket = ++sequence.current;
    const [result, contacts] = await Promise.all([sharedReading.status(), friendsApi.list()]);
    if (!mounted.current || ticket !== sequence.current) return;
    setLists(result.lists); setFriends(contacts.friends.filter((friend) => friend.status === "active")); setFresh(true);
  }, []);
  useEffect(() => {
    mounted.current = true;
    let running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      try { await load(); } catch { if (mounted.current) { setFresh(false); } }
      finally { running = false; }
    };
    void poll(); const timer = window.setInterval(() => void poll(), 5000);
    return () => { mounted.current = false; sequence.current++; window.clearInterval(timer); };
  }, [load]);
  const run = async (value: object) => {
    setBusy(true); setError(""); setNotice("");
    try {
      await sharedReading.action(value);
      if (!mounted.current) return true;
      setNotice("Saved on this node. Review pending acknowledgements below; this does not mean the other device has received it.");
      try { await load(); } catch { setFresh(false); }
      return true;
    } catch (cause) { if (mounted.current) setError(cause instanceof Error ? cause.message : "Could not confirm this operation. Retry."); return false; }
    finally { if (mounted.current) setBusy(false); }
  };
  return <div className="screen-stack">
    <PageHeader eyebrow="Private collaboration" title="Shared reading lists" context="Share only the links you add here and each member's read status. Your private reading history is separate." />
    <p>The creating node records edits in order. Both members can add or remove items and mark their own reading status. Offline changes wait for that node; either member can close the list.</p>
    {!fresh ? <p role="status">Current list status unavailable or loading. Reconnect to your node; retained values may be out of date.</p> : null}
    {error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    <Button disabled={busy} onClick={() => void load().catch(() => setFresh(false))}>Refresh lists</Button>
    <Panel><h2>Create or join a list</h2>
      <label>Friend<select value={rid} onChange={(event) => { setRid(event.target.value); setInvites([]); createId.current = newId(); }} disabled={busy}>
        <option value="">Choose an active friend</option>{friends.map((friend) => <option key={friend.relationship_id} value={friend.relationship_id}>{friend.node_name}</option>)}
      </select></label>
      <label>List title<input value={title} onChange={(event) => { setTitle(event.target.value); createId.current = newId(); }} disabled={busy} maxLength={300} /></label>
      <Button disabled={busy || !fresh || !rid || !title.trim()} onClick={() => void run({ action: "create", relationship_id: rid, title, id: createId.current }).then((done) => { if (done) { createId.current = newId(); setTitle(""); } })}>Create shared list invitation</Button>{" "}
      <Button disabled={busy || !fresh || !rid} onClick={() => {
        setBusy(true); setError(""); const reviewed = rid;
        void sharedReading.discover(reviewed).then((result) => { if (mounted.current) setInvites(result.invitations.map((row) => ({ ...row, rid: reviewed }))); })
          .catch((cause: Error) => { if (mounted.current) setError(cause.message); }).finally(() => { if (mounted.current) setBusy(false); });
      }}>Check invitations from friend</Button>
      <p>Ask your friend to open Shared reading lists and check invitations from you. No message is sent automatically.</p>
      {invites.map((invite) => <div key={invite.id}><strong>{invite.title}</strong>{" "}<Button disabled={busy} onClick={() => confirm({ title: `Join ${invite.title}?`, body: "This friend will see the links and read status you add to this list. Private reading history is not shared. Copies remain if the friendship ends.", risk: "medium", confirmLabel: "Join shared list", onConfirm: async () => { await run({ action: "accept", relationship_id: invite.rid, id: invite.id }); } })}>Review and join</Button></div>)}
    </Panel>
    {lists.map((list) => <ListCard key={list.id} list={list} busy={busy || !fresh} run={run} />)}
  </div>;
}
