import { useEffect, useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { Badge, Button, Card, ErrorBox, Field, Table, Tabs, Time, useAction, useDebounced, useLoad, useUi } from "../ui.jsx";

function Messages() {
  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const [onlyUnread, setOnlyUnread] = useState(false);
  const [st, setSt] = useState({ items: [], loading: true, error: null, next: null, unread: 0 });
  const a = useAction();
  const load = async (before) => {
    setSt((s) => ({ ...s, loading: true, error: null }));
    try {
      const r = await api.get("/v1/inbox", { q: dq, unread: onlyUnread || undefined, before_id: before || undefined, limit: 50 });
      setSt((s) => ({ items: before ? [...s.items, ...r.items] : r.items, loading: false, error: null, next: r.next_before_id, unread: r.unread }));
    } catch (e) { setSt((s) => ({ ...s, loading: false, error: e })); }
  };
  useEffect(() => { load(); }, [dq, onlyUnread]); // eslint-disable-line
  const markAll = async () => { await a.run(() => api.post("/v1/inbox/read", {}), t("All marked as read")); load(); };
  const reply = (to) => { try { sessionStorage.setItem("sms_prefill_to", `+${to}`); } catch { /* pa sessionStorage */ } location.hash = "send"; };
  return (
    <Card title={t("Received messages")} subtitle={t("SMS people send to your numbers. STOP and START are handled automatically.")}
      actions={<><span className="muted small">{t("{n} unread", { n: st.unread })}</span><Button busy={a.busy} disabled={!st.unread} onClick={markAll}>{t("Mark all as read")}</Button></>}>
      <div className="toolbar">
        <input aria-label={t("Search")} placeholder={t("Search by number or text")} value={q} onChange={(e) => setQ(e.target.value)} />
        <label className="chip"><input type="checkbox" checked={onlyUnread} onChange={(e) => setOnlyUnread(e.target.checked)} />{t("Unread only")}</label>
      </div>
      <ErrorBox error={st.error || a.error} retry={() => load()} />
      <Table rows={st.items} loading={st.loading} emptyTitle={q || onlyUnread ? t("No matches") : t("No messages yet")}
        empty={q || onlyUnread ? t("Try a different search.") : t("When someone replies to one of your numbers, it shows up here.")}
        footer={st.next && <div className="row end" style={{ padding: 12 }}><Button busy={st.loading} onClick={() => load(st.next)}>{t("Load more")}</Button></div>}
        cols={[{ label: t("From"), render: (r) => <b>{r.read_at ? "" : "● "}+{r.from}</b> },
          { label: t("Message"), render: (r) => <span>{r.text}{r.action && <> <Badge>{r.action === "opt_out" ? "opted_out" : "active"}</Badge></>}{r.keyword && <> <code>{r.keyword}</code></>}</span> },
          { label: t("Auto-reply"), render: (r) => (r.reply_status ? (r.reply_status === "queued" ? t("sent") : r.reply_status) : "") },
          { label: t("Received"), render: (r) => <Time value={r.created_at} /> },
          { label: "", render: (r) => <Button className="small" onClick={() => reply(r.from)}>{t("Reply")}</Button> }]} />
    </Card>
  );
}

function Keywords() {
  const { confirm } = useUi();
  const list = useLoad(() => api.get("/v1/keywords"), []);
  const [f, setF] = useState({ keyword: "", reply_text: "" });
  const a = useAction();
  const bad = f.keyword && !/^[A-Za-z0-9]{2,32}$/.test(f.keyword);
  return (
    <>
      <div className="help"><b>{t("What are keywords?")}</b> {t("When a message starts with a word you define (for example HELP), we can answer automatically. The reply is sent as a normal SMS and costs the usual price. STOP and START always work and can't be changed.")}</div>
      <Card title={t("Add a keyword")}>
        <div className="form">
          <div className="grid two">
            <Field label={t("Keyword")} hint={t("2-32 letters or digits, no spaces")} error={bad ? t("Use only letters and digits") : null}><input value={f.keyword} onChange={(e) => setF({ ...f, keyword: e.target.value })} placeholder="HELP" /></Field>
            <Field label={t("Automatic reply")} hint={t("Leave empty to only record the message")}><input maxLength={480} value={f.reply_text} onChange={(e) => setF({ ...f, reply_text: e.target.value })} /></Field>
          </div>
          <ErrorBox error={a.error} />
          <div><Button variant="primary" busy={a.busy} disabled={!f.keyword || bad} onClick={async () => { await a.run(() => api.put("/v1/keywords", f), t("Keyword saved")); setF({ keyword: "", reply_text: "" }); list.reload(); }}>{t("Save keyword")}</Button></div>
        </div>
      </Card>
      <Card title={t("Your keywords")}>
        <ErrorBox error={list.error} retry={list.reload} />
        <Table rows={list.data || []} loading={list.loading} emptyTitle={t("No keywords yet")} empty={t("Add one above.")}
          cols={[{ label: t("Keyword"), render: (r) => <code>{r.keyword}</code> }, { label: t("Automatic reply"), render: (r) => r.reply_text || <span className="muted">-</span> },
            { label: "", render: (r) => <Button variant="danger" className="small" onClick={async () => { if (await confirm({ title: t("Delete “{name}”?", { name: r.keyword }), body: t("Messages starting with this word will no longer get an automatic reply."), danger: true, confirmLabel: t("Delete") })) { await a.run(() => api.del(`/v1/keywords/${r.id}`), t("Keyword deleted")); list.reload(); } }}>{t("Delete")}</Button> }]} />
      </Card>
    </>
  );
}

export default function Inbox() {
  const [tab, setTab] = useState("messages");
  return (<><Tabs value={tab} onChange={setTab} tabs={[{ id: "messages", label: t("Messages") }, { id: "keywords", label: t("Keywords") }]} />{tab === "messages" ? <Messages /> : <Keywords />}</>);
}
