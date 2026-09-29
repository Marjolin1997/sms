import { useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, Field, Notice, Table, useAction, useLoad, when } from "../ui.jsx";

export default function Keys({ me }) {
  const staff = !me.owner_ref;
  const base = staff ? "/v1/admin/api-keys" : "/v1/portal/api-keys";
  const keys = useLoad(() => api.get(base), [base]);
  const [name, setName] = useState("");
  const [role, setRole] = useState("client");
  const [created, setCreated] = useState(null);
  const a = useAction(), b = useAction();
  return (
    <>
      {created && <div className="alert good"><b>New key (shown once):</b> <code>{created}</code> <Button onClick={() => setCreated(null)}>Hide</Button></div>}
      <Card title="Create API key">
        <div className="row wrap">
          <input placeholder="Key name, e.g. production server" value={name} onChange={(e) => setName(e.target.value)} />
          {staff && <select value={role} onChange={(e) => setRole(e.target.value)}>{["client", "finance", "pricing", "approver", "support", "superadmin"].map((r) => <option key={r}>{r}</option>)}</select>}
          <Button variant="primary" busy={a.busy} disabled={!name} onClick={async () => {
            const body = staff ? { name, role, owner_ref: role === "client" ? undefined : null } : { name };
            const r = await a.run(() => api.post(base, body), "Key created");
            if (r) { setCreated(r.key); setName(""); keys.reload(); }
          }}>Create</Button>
        </div>
        <Notice error={a.error} ok={a.ok} />
        <small className="muted">The full key is shown only once. We store just a hash.</small>
      </Card>
      <Card title="Keys">
        <Notice error={b.error || keys.error} ok={b.ok} />
        <Table rows={keys.data || []} empty="No keys." cols={[{ label: "Name", key: "name" }, { label: "Prefix", render: (r) => <code>sms_{r.prefix}…</code> }, ...(staff ? [{ label: "Role", key: "role" }, { label: "Account", render: (r) => r.owner_ref || "-" }] : []),
          { label: "Status", render: (r) => <Badge>{r.status}</Badge> }, { label: "Last used", render: (r) => when(r.last_used_at) },
          { label: "", render: (r) => r.status === "active" && <Button variant="danger" busy={b.busy} onClick={async () => { if (confirm("Revoke this key? Anything using it will stop working.")) { await b.run(() => api.post(`${base}/${r.id}/revoke`), "Revoked"); keys.reload(); } }}>Revoke</Button> }]} />
      </Card>
    </>
  );
}
