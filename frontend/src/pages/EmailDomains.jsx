import { useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, Field, Notice, Table, useAction, useLoad, when } from "../ui.jsx";

const Check = ({ ok, label }) => <span className={`check ${ok ? "ok" : ""}`}>{ok ? "✓" : "○"} {label}</span>;

export default function EmailDomains() {
  const domains = useLoad(() => api.get("/v1/email/domains"), []);
  const [domain, setDomain] = useState("");
  const a = useAction(), v = useAction();
  return (
    <>
      <Card title="Add a sending domain">
        <div className="row">
          <input placeholder="mail.example.com" value={domain} onChange={(e) => setDomain(e.target.value)} />
          <Button variant="primary" busy={a.busy} disabled={!domain} onClick={async () => { await a.run(() => api.post("/v1/email/domains", { domain }), "Domain added, publish the DNS records below"); setDomain(""); domains.reload(); }}>Add domain</Button>
        </div>
        <Notice error={a.error} ok={a.ok} />
        <small className="muted">You can only send from domains you have verified. We sign every message with DKIM.</small>
      </Card>
      {(domains.data || []).map((d) => (
        <Card key={d.id} title={d.domain} actions={<><Badge>{d.status}</Badge><Button busy={v.busy} onClick={async () => { await v.run(() => api.post(`/v1/email/domains/${d.id}/verify`), "Checked"); domains.reload(); }}>Verify DNS</Button></>}>
          <div className="row wrap"><Check ok={d.dkim_ok} label="DKIM" /><Check ok={d.spf_ok} label="SPF" /><Check ok={d.dmarc_ok} label="DMARC (optional)" /><small className="muted">Last check: {when(d.last_checked_at)}</small></div>
          {d.dns_records && (
            <Table rows={d.dns_records.map((r, i) => ({ ...r, id: i }))} cols={[
              { label: "Type", key: "type" }, { label: "Host", render: (r) => <code>{r.name}</code> },
              { label: "Value", render: (r) => <code className="wrap-code">{r.value}</code> },
              { label: "", render: (r) => (r.required ? <Badge>pending</Badge> : <span className="muted">optional</span>) }]} />
          )}
        </Card>
      ))}
      <Notice error={v.error || domains.error} ok={v.ok} />
      {domains.data && !domains.data.length && <div className="empty">No domains yet.</div>}
    </>
  );
}
