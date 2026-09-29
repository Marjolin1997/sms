import { useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, ErrorBox, Table, Tabs, Time, useAction, useDebounced, useLoad, useUi } from "../ui.jsx";

const SWITCH_HELP = { submit: "Stops accepting NEW messages. Callers get a clear 'paused' error.", dispatch: "Stops sending to providers. Messages queue up and go out when you resume." };

function Switches() {
  const { confirm } = useUi();
  const sw = useLoad(() => api.get("/v1/admin/switches"), [], 5000);
  const a = useAction();
  const toggle = async (s) => {
    if (s.enabled) {
      const reason = await confirm({ title: `Pause “${s.name}”?`, body: SWITCH_HELP[s.name], input: "Reason", placeholder: "e.g. Provider outage", inputRequired: true, minLength: 3, danger: true, confirmLabel: "Pause" });
      if (!reason) return;
      await a.run(() => api.put(`/v1/admin/switches/${s.name}`, { enabled: false, reason }), `${s.name} paused`);
    } else await a.run(() => api.put(`/v1/admin/switches/${s.name}`, { enabled: true }), `${s.name} resumed`);
    sw.reload();
  };
  return (
    <Card title="Kill switches" subtitle="Take effect immediately in every process.">
      <ErrorBox error={a.error || sw.error} retry={sw.reload} />
      <div className="grid stats">
        {(sw.data || []).map((s) => (
          <div key={s.name} className="stat">
            <div className="stat-l">{s.name}</div>
            <div className={`stat-v ${s.enabled ? "good" : "bad"}`}>{s.enabled ? "ON" : "PAUSED"}</div>
            <div className="stat-s">{s.enabled ? SWITCH_HELP[s.name] : `${s.reason} (by ${s.updated_by})`}</div>
            <Button variant={s.enabled ? "danger" : "primary"} busy={a.busy} onClick={() => toggle(s)}>{s.enabled ? "Pause" : "Resume"}</Button>
          </div>
        ))}
      </div>
    </Card>
  );
}

function Audit() {
  const [q, setQ] = useState("");
  const dq = useDebounced(q).toLowerCase();
  const audit = useLoad(() => api.get("/v1/admin/audit", { limit: 500 }), [], 8000);
  const rows = [...(audit.data || [])].reverse().filter((r) => !dq || `${r.action} ${r.actor} ${r.target_type} ${r.target_id}`.toLowerCase().includes(dq));
  return (
    <Card title="Audit log" subtitle="Every staff and customer change, newest first. Entries can't be edited or removed.">
      <div className="toolbar"><input aria-label="Filter the log" placeholder="Filter by action, person or item" value={q} onChange={(e) => setQ(e.target.value)} /></div>
      <ErrorBox error={audit.error} retry={audit.reload} />
      <Table rows={rows} loading={audit.loading} empty={q ? "No entries match." : "Nothing recorded yet."} cols={[{ label: "When", render: (r) => <Time value={r.at} /> }, { label: "Who", render: (r) => <><code>{r.actor}</code> <Badge>{r.role}</Badge></> }, { label: "Did", key: "action" }, { label: "To", render: (r) => `${r.target_type} ${r.target_id}` }, { label: "Details", render: (r) => (r.detail ? <small className="muted">{JSON.stringify(r.detail).slice(0, 90)}</small> : "") }]} />
    </Card>
  );
}

export default function Admin() {
  const [tab, setTab] = useState("switches");
  return (<><Tabs value={tab} onChange={setTab} tabs={[{ id: "switches", label: "Kill switches" }, { id: "audit", label: "Audit log" }]} />{tab === "switches" ? <Switches /> : <Audit />}</>);
}
