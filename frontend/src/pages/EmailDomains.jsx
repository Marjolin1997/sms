import { useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
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
      <div className="help"><b>{t("Why verify a domain?")}</b> {t("Mail providers only trust messages from senders who can prove they own the address. You add three DNS records once at your domain provider (where you bought the domain). We then sign every email so it's much less likely to end up in spam. It usually takes a few minutes, sometimes up to a day for DNS to update.")}</div>
      <Card title={t("Add a sending domain")}>
        <div className="row wrap">
          <Field label={t("Domain")} hint={t("For example mail.yourcompany.com, or yourcompany.com")} error={bad ? t("That doesn't look like a domain name") : null}><input value={domain} onChange={(e) => setDomain(e.target.value)} placeholder="mail.example.com" /></Field>
          <Button variant="primary" busy={a.busy} disabled={!domain || bad} onClick={async () => { await a.run(() => api.post("/v1/email/domains", { domain: domain.trim() }), t("Domain added. Now publish the DNS records below.")); setDomain(""); domains.reload(); }}>{t("Add domain")}</Button>
        </div>
        <ErrorBox error={a.error} />
      </Card>
      <ErrorBox error={domains.error || v.error} retry={domains.reload} />
      {(domains.data || []).map((d) => (
        <Card key={d.id} title={d.domain} subtitle={d.last_checked_at ? <>{t("Last checked")} <Time value={d.last_checked_at} /></> : t("Not checked yet")}
          actions={<><Badge>{d.status}</Badge><Button variant={d.status === "pending" ? "primary" : ""} busy={v.busy} onClick={async () => {
            const r = await v.run(() => api.post(`/v1/email/domains/${d.id}/verify`));
            domains.reload();
            if (r && r !== true) toast(r.status === "verified" ? t("Domain verified 🎉") : t("Not verified yet. DNS changes can take a while to spread. Check the records match exactly, then try again."), r.status === "verified" ? "good" : "info");
          }}>{d.status === "verified" ? t("Re-check") : t("Check my DNS")}</Button></>}>
          <div className="row wrap"><Check ok={d.dkim_ok} label={t("Signing key (DKIM)")} hint={t("Proves your emails weren't altered")} /><Check ok={d.spf_ok} label={t("Sender policy (SPF)")} hint={t("Says our servers may send for you")} /><Check ok={d.dmarc_ok} label={t("DMARC (optional)")} hint={t("Recommended for best deliverability")} /></div>
          {d.status === "pending" && d.dns_records && (
            <>
              <p className="muted">{t("Add these records at your DNS provider, then press “Check my DNS”. If you already have an SPF record, don't add a second one: add the include: part to your existing one.")}</p>
              <Table rows={d.dns_records.map((r, i) => ({ ...r, id: i }))} cols={[
                { label: t("Type"), key: "type" }, { label: t("Name / host"), render: (r) => <><code>{r.name}</code> <CopyButton text={r.name} /></> },
                { label: t("Value"), render: (r) => <><code className="wrap-code">{r.value}</code> <CopyButton text={r.value} /></> }, { label: "", render: (r) => (r.required ? <Badge>required</Badge> : <span className="muted">{t("optional")}</span>) }]} />
            </>
          )}
          {d.status === "verified" && <div className="alert good"><span>{t("You can send from any address at")} <b>{d.domain}</b>.</span></div>}
        </Card>
      ))}
      {domains.data && !domains.data.length && <div className="empty">{t("No domains yet. Add one above to start sending email.")}</div>}
    </>
  );
}
