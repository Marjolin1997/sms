import { useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, Notice, Stat, Table, useAction, useLoad, when } from "../ui.jsx";

export default function Admin() {
  const sw = useLoad(() => api.get("/v1/admin/switches"), [], 5000);
  const audit = useLoad(() => api.get("/v1/admin/audit", { limit: 200 }), [], 8000);
  const [reason, setReason] = useState("");
  const a = useAction();
  const toggle = async (s) => {
    const enabled = !s.enabled;
    if (!enabled && !reason.trim()) return alert("A reason is required to pause.");
    await a.run(() => api.put(`/v1/admin/switches/${s.name}`, { enabled, reason: enabled ? null : reason }), enabled ? "Resumed" : "Paused");
    sw.reload(); audit.reload();
  };
  return (
    <>
      <Card title="Kill switches">
        <p className="muted">Pausing <b>submit</b> rejects new messages immediately; pausing <b>dispatch</b> stops workers from contacting providers. Both take effect in every process at once.</p>
        <div className="row"><input placeholder="Reason (required to pause)" value={reason} onChange={(e) => setReason(e.target.value)} /></div>
        <Notice error={a.error || sw.error} ok={a.ok} />
        <div className="grid stats">
          {(sw.data || []).map((s) => (
            <div key={s.name} className="stat">
              <div className="stat-l">{s.name}</div>
              <div className={`stat-v ${s.enabled ? "" : "bad"}`}>{s.enabled ? "ON" : "PAUSED"}</div>
              {s.reason && <div className="stat-s">{s.reason} ({s.updated_by})</div>}
              <Button variant={s.enabled ? "danger" : "primary"} busy={a.busy} onClick={() => toggle(s)}>{s.enabled ? "Pause" : "Resume"}</Button>
            </div>
          ))}
        </div>
      </Card>
      <Card title="Audit log (latest 200)">
        <Table rows={[...(audit.data || [])].reverse()} empty="No audit entries." cols={[
          { label: "When", render: (r) => when(r.at) }, { label: "Actor", render: (r) => <code>{r.actor}</code> }, { label: "Role", render: (r) => <Badge>{r.role}</Badge> },
          { label: "Action", key: "action" }, { label: "Target", render: (r) => `${r.target_type} ${r.target_id}` }]} />
      </Card>
    </>
  );
}
