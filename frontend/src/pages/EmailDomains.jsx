import { useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, CopyButton, ErrorBox, Field, Table, Time, useAction, useLoad, useUi } from "../ui.jsx";

const Check = ({ ok, label, hint }) => <span className={`check ${ok ? "ok" : ""}`} title={hint}>{ok ? "✓" : "○"} {label}</span>;

export default function EmailDomains() {
  const { toast } = useUi();
  const domains = useLoad(() => api.get("/v1/email/domains"), []);
  const [domain, setDomain] = useState("");
  const a = useAction(), v = useAction();
  const bad = domain && !/^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$/i.test(domain.trim());
  return (
    <>
      <div className="help"><b>Why verify a domain?</b> Mail providers only trust messages from senders who can prove they own the address. You add three DNS records once at your domain provider (where you bought the domain). We then sign every email so it's much less likely to end up in spam. It usually takes a few minutes, sometimes up to a day for DNS to update.</div>
      <Card title="Add a sending domain">
        <div className="row wrap">
          <Field label="Domain" hint="For example mail.yourcompany.com, or yourcompany.com" error={bad ? "That doesn't look like a domain name" : null}><input value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="mail.example.com" /></Field>
          <Button variant="primary" busy={a.busy} disabled={!domain || bad} onClick={async () => { await a.run(() => api.post("/v1/email/domains", { domain: domain.trim() }), "Domain added. Now publish the DNS records below."); setDomain(""); domains.reload(); }}>Add domain</Button>
        </div>
        <ErrorBox error={a.error} />
      </Card>
      <ErrorBox error={domains.error || v.error} retry={domains.reload} />
      {(domains.data || []).map((d) => (
        <Card key={d.id} title={d.domain} subtitle={d.last_checked_at ? <>Last checked <Time value={d.last_checked_at} /></> : "Not checked yet"}
          actions={<><Badge>{d.status}</Badge><Button variant={d.status === "pending" ? "primary" : ""} busy={v.busy} onClick={async () => {
            const r = await v.run(() => api.post(`/v1/email/domains/${d.id}/verify`));
            domains.reload();
            if (r && r !== true) toast(r.status === "verified" ? "Domain verified 🎉" : "Not verified yet. DNS changes can take a while to spread. Check the records match exactly, then try again.", r.status === "verified" ? "good" : "info");
          }}>{d.status === "verified" ? "Re-check" : "Check my DNS"}</Button></>}>
          <div className="row wrap"><Check ok={d.dkim_ok} label="Signing key (DKIM)" hint="Proves your emails weren't altered" /><Check ok={d.spf_ok} label="Sender policy (SPF)" hint="Says our servers may send for you" /><Check ok={d.dmarc_ok} label="DMARC (optional)" hint="Recommended for best deliverability" /></div>
          {d.status === "pending" && d.dns_records && (
            <>
              <p className="muted">Add these records at your DNS provider, then press “Check my DNS”. If you already have an SPF record, don't add a second one: add the <code>include:</code> part to your existing one.</p>
              <Table rows={d.dns_records.map((r, i) => ({ ...r, id: i }))} cols={[
                { label: "Type", key: "type" }, { label: "Name / host", render: (r) => <><code>{r.name}</code> <CopyButton text={r.name} label="Copy" /></> },
                { label: "Value", render: (r) => <><code className="wrap-code">{r.value}</code> <CopyButton text={r.value} label="Copy" /></> }, { label: "", render: (r) => (r.required ? <Badge>required</Badge> : <span className="muted">optional</span>) }]} />
            </>
          )}
          {d.status === "verified" && <div className="alert good"><span>You can send from any address at <b>{d.domain}</b>.</span></div>}
        </Card>
      ))}
      {domains.data && !domains.data.length && <div className="empty">No domains yet. Add one above to start sending email.</div>}
    </>
  );
}
