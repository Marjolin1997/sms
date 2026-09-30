import { useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { Badge, Button, Card, ErrorBox, Field, SecretBanner, Table, Time, useAction, useLoad, useUi } from "../ui.jsx";

export default function Keys({ me }) {
  const { confirm } = useUi();
  const staff = !me.owner_ref;
  const base = staff ? "/v1/admin/api-keys" : "/v1/portal/api-keys";
  const keys = useLoad(() => api.get(base), [base]);
  const [name, setName] = useState("");
  const [role, setRole] = useState("client");
  const [cidrs, setCidrs] = useState("");
  const [created, setCreated] = useState(null);
  const a = useAction(), b = useAction();
  return (
    <>
      {created && <SecretBanner title={t("Copy your new key now")} note={t("This is the only time it's shown. We store just a fingerprint, so it can't be recovered. If you lose it, create a new one.")} value={created} onClose={() => setCreated(null)} />}
      <div className="help"><b>{t("Use a separate key for each system")}</b> {t("(website, backend, staging). If one leaks, you can revoke it without touching the others. Never put a key in a web page or mobile app.")}</div>
      <Card title={t("Create an API key")}>
        <div className="row wrap">
          <Field label={t("Name")} hint={t("So you remember where it's used")}><input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("Production server")} /></Field>
          {staff && <Field label={t("Role")}><select value={role} onChange={(e) => setRole(e.target.value)}>{["client", "finance", "pricing", "approver", "support", "superadmin"].map((r) => <option key={r} value={r}>{t(r)}</option>)}</select></Field>}
          <Field label={t("Allowed IP addresses (optional)")} hint={t("Comma-separated, e.g. 203.0.113.7, 198.51.100.0/24. Empty means any address.")}><input value={cidrs} onChange={(e) => setCidrs(e.target.value)} placeholder="203.0.113.7" /></Field>
          <Button variant="primary" busy={a.busy} disabled={!name.trim()} onClick={async () => { const allowed_cidrs = cidrs.split(",").map((x) => x.trim()).filter(Boolean); const r = await a.run(() => api.post(base, { ...(staff ? { name, role, owner_ref: role === "client" ? undefined : null } : { name }), allowed_cidrs }), t("Key created")); if (r && r.key) { setCreated(r.key); setName(""); setCidrs(""); keys.reload(); } }}>{t("Create key")}</Button>
        </div>
        <ErrorBox error={a.error} />
      </Card>
      <Card title={t("Your keys")}>
        <ErrorBox error={b.error || keys.error} retry={keys.reload} />
        <Table rows={keys.data || []} loading={keys.loading} empty={t("No keys yet.")} cols={[{ label: t("Name"), render: (r) => <b>{r.name}</b> }, { label: t("Starts with"), render: (r) => <code>sms_{r.prefix}…</code> }, ...(staff ? [{ label: t("Role"), render: (r) => t(r.role) }, { label: t("Account"), render: (r) => r.owner_ref || "-" }] : []),
          { label: t("Status"), render: (r) => <Badge>{r.status}</Badge> }, { label: t("Allowed IPs"), render: (r) => (r.allowed_cidrs?.length ? <code>{r.allowed_cidrs.join(", ")}</code> : t("any address")) }, { label: t("Last used"), render: (r) => (r.last_used_at ? <Time value={r.last_used_at} /> : t("never")) },
          { label: "", render: (r) => r.status === "active" && <div className="row wrap"><Button className="small" busy={b.busy} onClick={async () => { if (await confirm({ title: t("Rotate “{name}”?", { name: r.name }), body: t("You get a new key with the same access. The old key keeps working for 60 more minutes so you can switch over without downtime."), confirmLabel: t("Rotate key") })) { const x = await b.run(() => api.post(`${base}/${r.id}/rotate`, { grace_minutes: 60 }), t("New key created")); if (x && x.key) { setCreated(x.key); keys.reload(); } } }}>{t("Rotate")}</Button><Button variant="danger" className="small" busy={b.busy} onClick={async () => { if (await confirm({ title: t("Revoke “{name}”?", { name: r.name }), body: t("Anything using this key stops working immediately. This can't be undone."), danger: true, confirmLabel: t("Revoke key") })) { await b.run(() => api.post(`${base}/${r.id}/revoke`), t("Key revoked")); keys.reload(); } }}>{t("Revoke")}</Button></div> }]} />
      </Card>
    </>
  );
}
