import { useState } from "react";
import { T, t } from "../i18n.jsx";
import { api } from "../api.js";
import { Badge, Button, Card, ErrorBox, Table, Tabs, Time, useAction, useDebounced, useLoad, useUi } from "../ui.jsx";

const SWITCH_HELP = { submit: T("Stops accepting NEW messages. Callers get a clear 'paused' error."), dispatch: T("Stops sending to providers. Messages queue up and go out when you resume.") };
const SWITCH_NAME = { submit: T("Accepting messages"), dispatch: T("Sending to providers") };
const label = (n) => (SWITCH_NAME[n] ? t(SWITCH_NAME[n]) : n);
const help = (n) => (SWITCH_HELP[n] ? t(SWITCH_HELP[n]) : "");

function Switches() {
  const { confirm } = useUi();
  const sw = useLoad(() => api.get("/v1/admin/switches"), [], 5000);
  const a = useAction();
  const toggle = async (s) => {
    if (s.enabled) {
      const reason = await confirm({ title: t("Pause “{name}”?", { name: label(s.name) }), body: help(s.name), input: t("Reason"), placeholder: t("e.g. Provider outage"), inputRequired: true, minLength: 3, danger: true, confirmLabel: t("Pause") });
      if (!reason) return;
      await a.run(() => api.put(`/v1/admin/switches/${s.name}`, { enabled: false, reason }), t("{name} paused", { name: label(s.name) }));
    } else await a.run(() => api.put(`/v1/admin/switches/${s.name}`, { enabled: true }), t("{name} resumed", { name: label(s.name) }));
    sw.reload();
  };
  return (
    <Card title={t("Kill switches")} subtitle={t("Take effect immediately in every process.")}>
      <ErrorBox error={a.error || sw.error} retry={sw.reload} />
      <div className="grid stats">
        {(sw.data || []).map((s) => (
          <div key={s.name} className="stat">
            <div className="stat-l">{label(s.name)}</div>
            <div className={`stat-v ${s.enabled ? "good" : "bad"}`}>{s.enabled ? t("ON") : t("PAUSED")}</div>
            <div className="stat-s">{s.enabled ? help(s.name) : t("{reason} (by {who})", { reason: s.reason, who: s.updated_by })}</div>
            <Button variant={s.enabled ? "danger" : "primary"} busy={a.busy} onClick={() => toggle(s)}>{s.enabled ? t("Pause") : t("Resume")}</Button>
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
    <Card title={t("Audit log")} subtitle={t("Every staff and customer change, newest first. Entries can't be edited or removed.")}>
      <div className="toolbar"><input aria-label={t("Filter the log")} placeholder={t("Filter by action, person or item")} value={q} onChange={(e) => setQ(e.target.value)} /></div>
      <ErrorBox error={audit.error} retry={audit.reload} />
      <Table rows={rows} loading={audit.loading} empty={q ? t("No entries match.") : t("Nothing recorded yet.")} cols={[{ label: t("When"), render: (r) => <Time value={r.at} /> }, { label: t("Who"), render: (r) => <><code>{r.actor}</code> <Badge>{r.role}</Badge></> }, { label: t("Did"), key: "action" }, { label: t("Target"), render: (r) => `${r.target_type} ${r.target_id}` }, { label: t("Details"), render: (r) => (r.detail ? <small className="muted">{JSON.stringify(r.detail).slice(0, 90)}</small> : "") }]} />
    </Card>
  );
}

export default function Admin() {
  const [tab, setTab] = useState("switches");
  return (<><Tabs value={tab} onChange={setTab} tabs={[{ id: "switches", label: t("Kill switches") }, { id: "audit", label: t("Audit log") }]} />{tab === "switches" ? <Switches /> : <Audit />}</>);
}
