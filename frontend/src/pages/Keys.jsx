import { useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, ErrorBox, Field, SecretBanner, Table, Time, useAction, useLoad, useUi } from "../ui.jsx";

export default function Keys({ me }) {
  const { confirm } = useUi();
  const staff = !me.owner_ref;
  const base = staff ? "/v1/admin/api-keys" : "/v1/portal/api-keys";
  const keys = useLoad(() => api.get(base), [base]);
  const [name, setName] = useState("");
  const [role, setRole] = useState("client");
  const [created, setCreated] = useState(null);
  const a = useAction(), b = useAction();
  return (
    <>
      {created && <SecretBanner title="Copy your new key now" note="This is the only time it's shown. We store just a fingerprint, so it can't be recovered. If you lose it, create a new one." value={created} onClose={() => setCreated(null)} />}
      <div className="help"><b>Use a separate key for each system</b> (website, backend, staging). If one leaks, you can revoke it without touching the others. Never put a key in a web page or mobile app.</div>
      <Card title="Create an API key">
        <div className="row wrap">
          <Field label="Name" hint="So you remember where it's used"><input value={name} onChange={(e) => setName(e.target.value)} placeholder="Production server" /></Field>
          {staff && <Field label="Role"><select value={role} onChange={(e) => setRole(e.target.value)}>{["client", "finance", "pricing", "approver", "support", "superadmin"].map((r) => <option key={r}>{r}</option>)}</select></Field>}
          <Button variant="primary" busy={a.busy} disabled={!name.trim()} onClick={async () => { const r = await a.run(() => api.post(base, staff ? { name, role, owner_ref: role === "client" ? undefined : null } : { name }), "Key created"); if (r && r.key) { setCreated(r.key); setName(""); keys.reload(); } }}>Create key</Button>
        </div>
        <ErrorBox error={a.error} />
      </Card>
      <Card title="Your keys">
        <ErrorBox error={b.error || keys.error} retry={keys.reload} />
        <Table rows={keys.data || []} loading={keys.loading} empty="No keys yet." cols={[{ label: "Name", render: (r) => <b>{r.name}</b> }, { label: "Starts with", render: (r) => <code>sms_{r.prefix}…</code> }, ...(staff ? [{ label: "Role", key: "role" }, { label: "Account", render: (r) => r.owner_ref || "-" }] : []),
          { label: "Status", render: (r) => <Badge>{r.status}</Badge> }, { label: "Last used", render: (r) => (r.last_used_at ? <Time value={r.last_used_at} /> : "never") },
          { label: "", render: (r) => r.status === "active" && <Button variant="danger" className="small" busy={b.busy} onClick={async () => { if (await confirm({ title: `Revoke “${r.name}”?`, body: "Anything using this key stops working immediately. This can't be undone.", danger: true, confirmLabel: "Revoke key" })) { await b.run(() => api.post(`${base}/${r.id}/revoke`), "Key revoked"); keys.reload(); } }}>Revoke</Button> }]} />
      </Card>
    </>
  );
}
