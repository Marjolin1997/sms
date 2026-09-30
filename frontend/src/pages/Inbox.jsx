import { useEffect, useRef, useState } from "react";
import { api, uuid } from "../api.js";
import { Badge, Button, Card, Empty, ErrorBox, Skeleton, Time, smsInfo, useAction, useDebounced, useLoad } from "../ui.jsx";

const label = (t) => t.name || `+${t.number}`;

function ThreadList({ selected, onSelect, tick }) {
  const [q, setQ] = useState("");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const dq = useDebounced(q);
  const list = useLoad(() => api.get("/v1/inbox/threads", { q: dq, unread_only: unreadOnly ? "true" : "", limit: 50 }), [dq, unreadOnly, tick], 10000);
  const items = list.data?.items || [];
  return (
    <Card className="inbox-list">
      <div className="toolbar">
        <input aria-label="Search by number" placeholder="Search by number" value={q} onChange={(e) => setQ(e.target.value)} />
        <label className="chip"><input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} /> Unread only</label>
      </div>
      <ErrorBox error={list.error} retry={list.reload} />
      {list.loading && !list.data && <Skeleton rows={4} />}
      {list.data && items.length === 0 && (
        <Empty icon="✉" title={q || unreadOnly ? "No matches" : "No replies yet"}>
          {q || unreadOnly ? "Clear the filter to see every conversation." : "When someone replies to one of your messages, the conversation appears here."}
        </Empty>
      )}
      <ul className="threads" aria-label="Conversations">
        {items.map((t) => (
          <li key={t.number}>
            <button className={`thread ${selected === t.number ? "on" : ""} ${t.unread ? "unread" : ""}`} aria-current={selected === t.number ? "true" : undefined} onClick={() => onSelect(t.number)}>
              <span className="thread-top"><b>{label(t)}</b><small className="muted"><Time value={t.last_at} /></small></span>
              <span className="thread-prev">{t.keyword_action ? <Badge>{t.keyword_action}</Badge> : null} {t.preview}</span>
              {t.unread > 0 && <span className="count" aria-label={`${t.unread} unread`}>{t.unread}</span>}
            </button>
          </li>
        ))}
      </ul>
      {list.data?.next_before_id && <small className="muted">Showing the 50 most recent. Search to find older ones.</small>}
    </Card>
  );
}

const BLOCK = {
  "blocked:stop_keyword": "This person replied STOP, so you can't message them. If they reply START, you can again.",
  "blocked:erasure": "This contact was erased at their request, so you can't message them.",
};

function Bubble({ m }) {
  return (
    <div className={`bubble ${m.direction}`}>
      <div>{m.text}</div>
      <small>
        {m.direction === "out" && m.status && <Badge>{m.status}</Badge>} <Time value={m.at} />
        {m.keyword_action && <> · handled automatically as {m.keyword_action}</>}
        {m.error_code && <> · {m.error_code}</>}
      </small>
    </div>
  );
}

function Conversation({ number, onBack, onChanged }) {
  const conv = useLoad(() => api.get(`/v1/inbox/threads/${number}`), [number], 5000);
  const senders = useLoad(() => api.get("/v1/sender-ids", { status: "approved" }).catch(() => []), []);
  const [text, setText] = useState("");
  const [sender, setSender] = useState("");
  const a = useAction();
  const end = useRef(null);
  const c = conv.data;
  const count = c?.items.length;
  const unread = useRef(0);

  useEffect(() => { setText(""); setSender(""); }, [number]);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [count, number]);
  // Hapja e bisedës e shënon si të lexuar (edhe kur vjen mesazh i ri ndërsa është hapur).
  useEffect(() => {
    if (!c) return;
    const inCount = c.items.filter((i) => i.direction === "in").length;
    if (inCount !== unread.current) {
      unread.current = inCount;
      api.post(`/v1/inbox/threads/${number}/read`).then(onChanged).catch(() => {});
    }
  }, [c]); // eslint-disable-line
  useEffect(() => { unread.current = 0; }, [number]);

  const approved = [...new Set((senders.data || []).map((s) => s.value))];
  const from = sender || (c?.reply_from && approved.includes(c.reply_from) ? c.reply_from : approved[0] || "");
  const info = smsInfo(text);
  const blocked = c?.blocked && (BLOCK[c.blocked] || "You can't message this number right now.");

  const send = async (e) => {
    e?.preventDefault();
    if (!text.trim() || !from || blocked) return;
    const r = await a.run(() => api.post("/v1/messages", { to: `+${number}`, sender: from, text, category: "transactional" }, { headers: { "Idempotency-Key": uuid() } }), "Reply sent");
    if (r) { setText(""); conv.reload(); onChanged?.(); }
  };

  return (
    <Card className="inbox-conv" title={c ? (c.contact?.name || `+${number}`) : `+${number}`} subtitle={c?.contact?.name ? `+${number}` : c?.contact ? "Saved contact" : "Not in your contacts"}
      actions={<Button className="inbox-back" onClick={onBack}>← Back</Button>}>
      <ErrorBox error={conv.error} retry={conv.reload} />
      {conv.loading && !c && <Skeleton rows={4} />}
      {c && (
        <>
          <div className="bubbles" role="log" aria-live="polite" aria-label="Messages">
            {c.items.length === 0 && <Empty title="No messages">Nothing to show for this number.</Empty>}
            {c.items.map((m) => <Bubble key={`${m.direction}${m.id}`} m={m} />)}
            <div ref={end} />
          </div>
          {blocked && <div className="alert warn">{blocked}</div>}
          {!blocked && approved.length === 0 && senders.data && (
            <div className="alert warn"><span>You need an approved sender ID to reply.</span><a className="btn small primary" href="#senders">Request one</a></div>
          )}
          <form className="reply" onSubmit={send}>
            <textarea aria-label="Your reply" rows={2} placeholder={blocked ? "Replies are turned off for this number" : "Write a reply. Ctrl+Enter to send"} value={text} disabled={!!blocked} onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") send(e); }} />
            <div className="row wrap">
              {approved.length > 1 && (
                <select aria-label="Send from" value={from} onChange={(e) => setSender(e.target.value)}>{approved.map((s) => <option key={s}>{s}</option>)}</select>
              )}
              <small className="muted">{text ? `${info.length} characters · ${info.segments} part${info.segments === 1 ? "" : "s"}` : from ? `From ${from}` : ""}</small>
              <span style={{ flex: 1 }} />
              <Button variant="primary" busy={a.busy} disabled={!text.trim() || !from || !!blocked}>Send reply</Button>
            </div>
            <ErrorBox error={a.error} />
          </form>
        </>
      )}
    </Card>
  );
}

export default function Inbox({ onUnreadChange }) {
  const [number, setNumber] = useState(() => decodeURIComponent(location.hash.split("/")[1] || ""));
  const open = (n) => { setNumber(n); history.replaceState(null, "", n ? `#inbox/${n}` : "#inbox"); };
  const [tick, setTick] = useState(0);
  const changed = () => { setTick((t) => t + 1); onUnreadChange?.(); };
  return (
    <div className={`inbox ${number ? "has-conv" : ""}`}>
      <ThreadList selected={number} onSelect={open} tick={tick} />
      {number ? <Conversation number={number} onBack={() => open("")} onChanged={changed} /> : (
        <Card className="inbox-conv inbox-placeholder"><Empty icon="✉" title="Pick a conversation">Replies from your recipients show up on the left. Select one to read it and answer.</Empty></Card>
      )}
    </div>
  );
}
