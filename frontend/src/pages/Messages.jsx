import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, ErrorBox, Table, Tabs, Time, money, useDebounced } from "../ui.jsx";

const SMS_STATUS = ["", "queued", "sent", "delivered", "failed"];
const EMAIL_STATUS = ["", "queued", "sent", "delivered", "bounced", "complained", "failed"];

function Timeline({ path }) {
  const [rows, setRows] = useState(null);
  useEffect(() => { api.get(path).then(setRows).catch(() => setRows([])); }, [path]);
  return <Table rows={rows || []} loading={!rows} empty="No history." cols={[{ label: "Status", render: (r) => <Badge>{r.to}</Badge> }, { label: "Detail", render: (r) => r.detail || "" }]} />;
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
    ? [{ label: "To", render: (r) => <b>+{r.to}</b> }, { label: "From", key: "sender" }, { label: "Status", render: (r) => <Badge>{r.status}</Badge> }, { label: "Type", key: "category" },
       { label: "Parts", key: "segments", num: true }, { label: "Cost", num: true, render: (r) => `${money(r.total_price)} ${r.currency}` }, { label: "Note", render: (r) => r.error_code || "" }, { label: "Sent", render: (r) => <Time value={r.created_at} /> }]
    : [{ label: "To", render: (r) => <b>{r.to}</b> }, { label: "Subject", key: "subject" }, { label: "Status", render: (r) => <Badge>{r.status}</Badge> }, { label: "Type", key: "category" }, { label: "Note", render: (r) => r.error_code || "" }, { label: "Sent", render: (r) => <Time value={r.created_at} /> }];
  const rows = state.items.map((r) => ({ ...r, _onClick: () => setOpen(open === r.id ? null : r.id) }));

  return (
    <>
      <Tabs tabs={[{ id: "sms", label: "SMS" }, { id: "email", label: "Email" }]} value={kind} onChange={(k) => { setKind(k); setStatus(""); setQ(""); }} />
      <Card>
        <div className="toolbar">
          <input aria-label="Search" placeholder={sms ? "Search by phone number" : "Search by address or subject"} value={q} onChange={(e) => setQ(e.target.value)} />
          <select aria-label="Filter by status" value={status} onChange={(e) => setStatus(e.target.value)}>
            {(sms ? SMS_STATUS : EMAIL_STATUS).map((s) => <option key={s} value={s}>{s ? `Status: ${s}` : "All statuses"}</option>)}
          </select>
          <Button onClick={() => load()}>Refresh</Button>
        </div>
        <ErrorBox error={state.error} retry={() => load()} />
        <Table rows={rows} loading={state.loading} emptyTitle={q || status ? "No matches" : "No messages yet"} empty={q || status ? "Try a different search or clear the filter." : "Messages you send appear here with their delivery status."} cols={cols}
          footer={state.next && <div className="row end" style={{ padding: 12 }}><Button busy={state.loading} onClick={() => load(state.next)}>Load older</Button></div>} />
      </Card>
      {open && (
        <Card title="What happened" subtitle={`Reference ${open}`} actions={<Button onClick={() => setOpen(null)}>Close</Button>}>
          <Timeline path={sms ? `/v1/messages/${open}/events` : `/v1/email/messages/${open}/events`} />
        </Card>
      )}
    </>
  );
}
