import { useState } from "react";
import { api, openHtml } from "../api.js";
import { Badge, Button, Card, ErrorBox, Field, Stat, Table, Time, money, useAction, useLoad, useUi, when } from "../ui.jsx";

function Subscription({ sub }) {
  if (!sub) return <Card title="Your plan"><div className="empty"><b>You're on pay-as-you-go</b><div>SMS is paid from your wallet. If you'd like a plan with included emails, contact us.</div></div></Card>;
  const u = sub.usage;
  const pct = u.included ? Math.min(100, (u.emails / u.included) * 100) : 0;
  return (
    <Card title="Your plan" actions={<Badge>{sub.status}</Badge>}>
      <div className="grid stats">
        <Stat label="Plan" value={sub.plan.name} sub={`${money(sub.plan.monthly_fee)} ${sub.plan.currency} per month`} />
        <Stat label="This period ends" value={new Date(sub.period_end).toLocaleDateString()} sub={`started ${new Date(sub.period_start).toLocaleDateString()}`} />
        <Stat label="Extra emails so far" value={`${money(u.overage_amount)} ${sub.plan.currency}`} tone={u.overage ? "warn" : "good"} sub={u.overage ? `${u.overage} above your included emails` : "Within your included emails"} />
      </div>
      <Field label={`Emails this period: ${u.emails.toLocaleString()} of ${u.included.toLocaleString()} included`}><div className="bar"><div className={`fill ${pct >= 100 ? "failed" : ""}`} style={{ width: `${pct}%` }} /></div></Field>
      {sub.pending_plan && <div className="alert warn"><span>You'll move to <b>{sub.pending_plan.name}</b> when this period ends.</span></div>}
      {sub.cancel_at_period_end && <div className="alert warn"><span>Your plan ends when this period ends.</span></div>}
      <small className="muted">{sub.auto_pay ? "Invoices are paid from your wallet automatically when there's enough balance." : "Invoices are not paid automatically."} Plan changes take effect from the next period.</small>
    </Card>
  );
}

function Profile({ profile, reload }) {
  const [f, setF] = useState(profile || { legal_name: "", address: "", country: "", email: "", tax_id: "" });
  const a = useAction();
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const bad = f.country && !/^[A-Za-z]{2}$/.test(f.country);
  return (
    <Card title="Billing details" subtitle="Printed on your invoices" actions={profile && <small className="muted">VAT: {(Number(profile.vat_rate) * 100).toFixed(2)}% (set by us)</small>}>
      <form className="form" onSubmit={async (e) => { e.preventDefault(); await a.run(() => api.put("/v1/billing/profile", { legal_name: f.legal_name, address: f.address, country: f.country.toUpperCase(), email: f.email, tax_id: f.tax_id || null }), "Billing details saved"); reload(); }}>
        <div className="grid two">
          <Field label="Company or legal name"><input required value={f.legal_name} onChange={set("legal_name")} /></Field>
          <Field label="Where to send invoices"><input required type="email" value={f.email} onChange={set("email")} /></Field>
          <Field label="Country" hint="Two-letter code, e.g. AL" error={bad ? "Two letters, like AL or XK" : null}><input required maxLength={2} value={f.country} onChange={set("country")} /></Field>
          <Field label="Tax ID / VAT number"><input value={f.tax_id || ""} onChange={set("tax_id")} /></Field>
        </div>
        <Field label="Address"><input required value={f.address} onChange={set("address")} /></Field>
        <ErrorBox error={a.error} />
        <div><Button variant="primary" busy={a.busy} disabled={bad}>Save details</Button></div>
        <small className="muted">Invoices already issued keep the details they were issued with.</small>
      </form>
    </Card>
  );
}

function Invoices({ reloadAll }) {
  const { toast } = useUi();
  const inv = useLoad(() => api.get("/v1/billing/invoices"), []);
  const a = useAction();
  const pay = (r, kind) => a.run(async () => {
    if (kind === "wallet") await api.post(`/v1/billing/invoices/${r.id}/pay-from-wallet`);
    else { const p = await api.post("/v1/billing/payments", { purpose: "invoice", invoice_id: r.id }); window.open(p.checkout_url, "_blank", "noopener"); toast("Payment page opened in a new tab", "info"); }
    inv.reload(); reloadAll();
  }, kind === "wallet" ? "Invoice paid from your wallet" : null);
  return (
    <Card title="Invoices">
      <ErrorBox error={a.error || inv.error} retry={inv.reload} />
      <Table rows={inv.data || []} loading={inv.loading} emptyTitle="No invoices yet" empty="Your first invoice is issued when your first billing period ends." cols={[
        { label: "Invoice", render: (r) => <b>{r.number}</b> },
        { label: "Period", render: (r) => `${new Date(r.period_start).toLocaleDateString()} – ${new Date(r.period_end).toLocaleDateString()}` },
        { label: "Total", num: true, render: (r) => `${money(r.total)} ${r.currency}` }, { label: "of which VAT", num: true, render: (r) => money(r.tax) },
        { label: "Due", render: (r) => new Date(r.due_at).toLocaleDateString() }, { label: "Status", render: (r) => <Badge>{r.status}</Badge> },
        { label: "", render: (r) => (
          <div className="row wrap">
            <Button className="small" onClick={() => a.run(() => openHtml(`/v1/billing/invoices/${r.id}/html`))}>View / print</Button>
            {r.status === "open" && <Button className="small" busy={a.busy} onClick={() => pay(r, "wallet")}>Pay from wallet</Button>}
            {r.status === "open" && <Button className="small" variant="primary" busy={a.busy} onClick={() => pay(r, "online")}>Pay online</Button>}
          </div>) }]} />
    </Card>
  );
}

function Staff({ reloadAll }) {
  const plans = useLoad(() => api.get("/v1/admin/billing/plans"), []);
  const overdue = useLoad(() => api.get("/v1/admin/billing/overdue"), [], 10000);
  const { confirm, toast } = useUi();
  const [f, setF] = useState({ code: "", name: "", currency: "EUR", monthly_fee: "", included_emails: 0, email_overage_price: "0" });
  const a = useAction(), c = useAction();
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  return (
    <>
      <Card title="Plans" subtitle="A plan can't be edited once created; make a new one and move customers to it." actions={<Button busy={c.busy} onClick={async () => { if (await confirm({ title: "Issue due invoices now?", body: "This normally runs by itself every 10 minutes.", confirmLabel: "Run now" })) { const r = await c.run(() => api.post("/v1/admin/billing/run")); if (r && r !== true) { toast(`${r.issued} invoice(s) issued`); reloadAll(); } } }}>Run billing now</Button>}>
        <Table rows={plans.data || []} loading={plans.loading} empty="No plans yet." cols={[{ label: "Code", key: "code" }, { label: "Name", key: "name" }, { label: "Fee / month", render: (r) => `${money(r.monthly_fee)} ${r.currency}` }, { label: "Included emails", key: "included_emails", num: true }, { label: "Extra email price", render: (r) => r.email_overage_price }, { label: "Status", render: (r) => <Badge>{r.status}</Badge> }]} />
        <h4>Create a plan</h4>
        <div className="grid two">
          <Field label="Code" hint="lowercase, e.g. growth"><input value={f.code} onChange={set("code")} /></Field><Field label="Name"><input value={f.name} onChange={set("name")} /></Field>
          <Field label="Monthly fee"><input inputMode="decimal" value={f.monthly_fee} onChange={set("monthly_fee")} /></Field><Field label="Currency"><input maxLength={3} value={f.currency} onChange={set("currency")} /></Field>
          <Field label="Emails included per month"><input type="number" min="0" value={f.included_emails} onChange={set("included_emails")} /></Field><Field label="Price per extra email"><input inputMode="decimal" value={f.email_overage_price} onChange={set("email_overage_price")} /></Field>
        </div>
        <ErrorBox error={a.error || c.error} />
        <div><Button busy={a.busy} disabled={!f.code || !f.name || f.monthly_fee === ""} onClick={async () => { await a.run(() => api.post("/v1/admin/billing/plans", { ...f, included_emails: Number(f.included_emails) }), "Plan created"); plans.reload(); }}>Create plan</Button></div>
      </Card>
      <Card title="Overdue invoices"><Table rows={overdue.data || []} empty="Nothing overdue." cols={[{ label: "Account", key: "owner_ref" }, { label: "Invoice", key: "number" }, { label: "Total", render: (r) => `${money(r.total)} ${r.currency}` }, { label: "Was due", render: (r) => when(r.due_at) }]} /></Card>
    </>
  );
}

export default function Billing({ me }) {
  const sub = useLoad(() => api.get("/v1/billing/subscription"), []);
  const prof = useLoad(() => api.get("/v1/billing/profile"), []);
  const pays = useLoad(() => api.get("/v1/billing/payments"), [], 6000);
  const reloadAll = () => { sub.reload(); prof.reload(); pays.reload(); };
  const staff = me.permissions.includes("billing:admin") || me.permissions.includes("*");
  const canEditProfile = me.permissions.includes("billing:profile") || me.permissions.includes("*");
  return (
    <>
      <Subscription sub={sub.data} />
      <Invoices reloadAll={reloadAll} />
      {canEditProfile && !prof.loading && <Profile key={JSON.stringify(prof.data)} profile={prof.data} reload={reloadAll} />}
      <Card title="Online payments"><Table rows={pays.data || []} loading={pays.loading} empty="No online payments yet." cols={[{ label: "For", key: "purpose" }, { label: "Amount", render: (r) => `${money(r.amount)} ${r.currency}` }, { label: "Status", render: (r) => <Badge>{r.status}</Badge> }, { label: "Note", render: (r) => ({ invoice_already_settled: "Invoice was already paid, so this went to your wallet", amount_mismatch: "Amount didn't match, being checked by us", timeout: "Expired" }[r.failure_reason] || r.failure_reason || "") }, { label: "Created", render: (r) => <Time value={r.created_at} /> }]} /></Card>
      {staff && <Staff reloadAll={reloadAll} />}
    </>
  );
}
