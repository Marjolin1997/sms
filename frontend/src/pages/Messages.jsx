import { useEffect, useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { Badge, Button, Card, ErrorBox, Table, Tabs, Time, money, useDebounced } from "../ui.jsx";

const SMS_STATUS = ["", "queued", "sent", "delivered", "failed"];
const EMAIL_STATUS = ["", "queued", "sent", "delivered", "bounced", "complained", "failed"];

function Timeline({ path }) {
  const [rows, setRows] = useState(null);
  useEffect(() => { api.get(path).then(setRows).catch(() => setRows([])); }, [path]);
  return <Table rows={rows || []} loading={!rows} empty={t("No history.")} cols={[{ label: t("Status"), render: (r) => <Badge>{r.to}</Badge> }, { label: t("Detail"), render: (r) => r.detail || "" }]} />;
}

export default function Messages() {
  const [kind, setKind] = useState("sms");
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const dq = useDebounced(q);
  const [state, setState] = useState({ items: [], next: null, loading: true, error: null });
  const [open, setOpen] = useState(null);
  const path = kind === "sms" ? "/v1/messages" : "/v1/email/messages";

  const load = async (before) => {
    setState((s) => ({ ...s, loading: true, error: null }));
    try {
      const r = await api.get(path, { status, q: dq, before_id: before, limit: 30 });
      setState((s) => ({ items: before ? [...s.items, ...r.items] : r.items, next: r.next_before_id, loading: false, error: null }));
    } catch (e) { setState((s) => ({ ...s, loading: false, error: e })); }
  };
  useEffect(() => { setOpen(null); load(); }, [kind, status, dq]); // eslint-disable-line

  const sms = kind === "sms";
  const cols = sms
    ? [{ label: t("To"), render: (r) => <b>+{r.to}</b> }, { label: t("From"), key: "sender" }, { label: t("Status"), render: (r) => <Badge>{r.status}</Badge> }, { label: t("Type"), render: (r) => t(r.category) },
       { label: t("Parts"), key: "segments", num: true }, { label: t("Cost"), num: true, render: (r) => `${money(r.total_price)} ${r.currency}` }, { label: t("Note"), render: (r) => r.error_code || "" }, { label: t("Sent"), render: (r) => <Time value={r.created_at} /> }]
    : [{ label: t("To"), render: (r) => <b>{r.to}</b> }, { label: t("Subject"), key: "subject" }, { label: t("Status"), render: (r) => <Badge>{r.status}</Badge> }, { label: t("Type"), render: (r) => t(r.category) }, { label: t("Note"), render: (r) => r.error_code || "" }, { label: t("Sent"), render: (r) => <Time value={r.created_at} /> }];
  const rows = state.items.map((r) => ({ ...r, _onClick: () => setOpen(open === r.id ? null : r.id) }));

  return (
    <>
      <Tabs tabs={[{ id: "sms", label: "SMS" }, { id: "email", label: "Email" }]} value={kind} onChange={(k) => { setKind(k); setStatus(""); setQ(""); }} />
      <Card>
        <div className="toolbar">
          <input aria-label={t("Search")} placeholder={sms ? t("Search by phone number") : t("Search by address or subject")} value={q} onChange={(e) => setQ(e.target.value)} />
          <select aria-label={t("Filter by status")} value={status} onChange={(e) => setStatus(e.target.value)}>
            {(sms ? SMS_STATUS : EMAIL_STATUS).map((s) => <option key={s} value={s}>{s ? t("Status: {status}", { status: t(s) }) : t("All statuses")}</option>)}
          </select>
          <Button onClick={() => load()}>{t("Refresh")}</Button>
        </div>
        <ErrorBox error={state.error} retry={() => load()} />
        <Table rows={rows} loading={state.loading} emptyTitle={q || status ? t("No matches") : t("No messages yet")} empty={q || status ? t("Try a different search or clear the filter.") : t("Messages you send appear here with their delivery status.")} cols={cols}
          footer={state.next && <div className="row end" style={{ padding: 12 }}><Button busy={state.loading} onClick={() => load(state.next)}>{t("Load older")}</Button></div>} />
      </Card>
      {open && (
        <Card title={t("What happened")} subtitle={t("Reference {id}", { id: open })} actions={<Button onClick={() => setOpen(null)}>{t("Close")}</Button>}>
          <Timeline path={sms ? `/v1/messages/${open}/events` : `/v1/email/messages/${open}/events`} />
        </Card>
      )}
    </>
  );
}
