import { useState } from "react";
import { api, openHtml } from "../api.js";
import { Badge, Button, Card, Field, Notice, Stat, Table, money, useAction, useLoad, when } from "../ui.jsx";

function Subscription({ sub }) {
  if (!sub) return <Card title="Subscription"><div className="empty">No subscription yet. Pay-as-you-go SMS from your wallet still works.</div></Card>;
  const u = sub.usage;
  const pct = u.included ? Math.min(100, (u.emails / u.included) * 100) : 0;
  return (
    <Card title="Subscription" actions={<Badge>{sub.status}</Badge>}>
      <div className="grid stats">
        <Stat label="Plan" value={sub.plan.name} sub={`${money(sub.plan.monthly_fee)} ${sub.plan.currency} / month`} />
        <Stat label="Current period" value={new Date(sub.period_end).toLocaleDateString()} sub={`from ${new Date(sub.period_start).toLocaleDateString()}`} />
        <Stat label="Overage so far" value={`${money(u.overage_amount)} ${sub.plan.currency}`} tone={u.overage ? "warn" : "good"} sub={`${u.overage} emails above quota`} />
      </div>
      <Field label={`Emails this period: ${u.emails.toLocaleString()} of ${u.included.toLocaleString()} included`}>
        <div className="bar"><div className={`fill ${pct >= 100 ? "failed" : ""}`} style={{ width: `${pct}%` }} /></div>
      </Field>
      {sub.pending_plan && <div className="alert warn">Switching to <b>{sub.pending_plan.name}</b> from the next period.</div>}
      {sub.cancel_at_period_end && <div className="alert warn">Cancels at the end of this period.</div>}
      <small className="muted">Auto-pay from wallet: {sub.auto_pay ? "on" : "off"}. Plan changes take effect next period; there is no proration.</small>
    </Card>
  );
}

function Profile({ profile, reload }) {
  const [f, setF] = useState(profile || { legal_name: "", address: "", country: "", email: "", tax_id: "" });
  const a = useAction();
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  return (
    <Card title="Billing details" actions={profile && <small className="muted">VAT rate: {(Number(profile.vat_rate) * 100).toFixed(2)}% (set by us)</small>}>
      <div className="form">
        <div className="grid two">
          <Field label="Legal name"><input value={f.legal_name} onChange={set("legal_name")} /></Field>
          <Field label="Billing email"><input type="email" value={f.email} onChange={set("email")} /></Field>
          <Field label="Country (ISO)"><input maxLength={2} value={f.country} onChange={set("country")} /></Field>
          <Field label="Tax ID"><input value={f.tax_id || ""} onChange={set("tax_id")} /></Field>
        </div>
        <Field label="Address"><input value={f.address} onChange={set("address")} /></Field>
        <Notice error={a.error} ok={a.ok} />
        <Button variant="primary" busy={a.busy} onClick={async () => { await a.run(() => api.put("/v1/billing/profile", { legal_name: f.legal_name, address: f.address, country: f.country, email: f.email, tax_id: f.tax_id || null }), "Saved"); reload(); }}>Save</Button>
        <small className="muted">Invoices already issued keep the details they were issued with.</small>
      </div>
    </Card>
  );
}

function Invoices({ reloadAll }) {
  const inv = useLoad(() => api.get("/v1/billing/invoices"), []);
  const a = useAction();
  const pay = (r, kind) => a.run(async () => {
    if (kind === "wallet") await api.post(`/v1/billing/invoices/${r.id}/pay-from-wallet`);
    else { const p = await api.post("/v1/billing/payments", { purpose: "invoice", invoice_id: r.id }); window.open(p.checkout_url, "_blank", "noopener"); }
    inv.reload(); reloadAll();
  }, kind === "wallet" ? "Paid from wallet" : "Payment page opened");
  return (
    <Card title="Invoices">
      <Notice error={a.error || inv.error} ok={a.ok} />
      <Table rows={inv.data || []} empty="No invoices yet. The first one is issued at the end of the first billing period." cols={[
        { label: "Number", render: (r) => <b>{r.number}</b> },
        { label: "Period", render: (r) => `${new Date(r.period_start).toLocaleDateString()} – ${new Date(r.period_end).toLocaleDateString()}` },
        { label: "Total", num: true, render: (r) => `${money(r.total)} ${r.currency}` }, { label: "VAT", num: true, render: (r) => money(r.tax) },
        { label: "Due", render: (r) => new Date(r.due_at).toLocaleDateString() }, { label: "Status", render: (r) => <Badge>{r.status}</Badge> },
        { label: "", render: (r) => (
          <div className="row">
            <Button onClick={() => a.run(() => openHtml(`/v1/billing/invoices/${r.id}/html`), "Invoice opened")}>View</Button>
            {r.status === "open" && <Button busy={a.busy} onClick={() => pay(r, "wallet")}>Pay from wallet</Button>}
            {r.status === "open" && <Button variant="primary" busy={a.busy} onClick={() => pay(r, "online")}>Pay online</Button>}
          </div>) }]} />
    </Card>
  );
}

function TopUp({ wallets, reloadAll }) {
  const [amount, setAmount] = useState("25");
  const [wallet, setWallet] = useState(wallets?.[0]?.id || "");
  const [link, setLink] = useState(null);
  const a = useAction();
  if (!wallets || !wallets.length) return null;
  return (
    <Card title="Top up wallet online">
      <div className="row wrap">
        <select value={wallet} onChange={(e) => setWallet(e.target.value)}>{wallets.map((w) => <option key={w.id} value={w.id}>{w.currency} wallet (balance {money(w.available)})</option>)}</select>
        <input value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="Amount" />
        <Button variant="primary" busy={a.busy} onClick={async () => { const p = await a.run(() => api.post("/v1/billing/payments", { purpose: "topup", wallet_id: Number(wallet), amount }), "Payment created"); if (p) { setLink(p.checkout_url); reloadAll(); } }}>Continue to payment</Button>
      </div>
      <Notice error={a.error} ok={a.ok} />
      {link && <div className="alert good">Complete your payment here: <a href={link} target="_blank" rel="noopener noreferrer">{link}</a><br /><small>Your balance is credited automatically once the payment is confirmed.</small></div>}
    </Card>
  );
}

function Staff({ reloadAll }) {
  const plans = useLoad(() => api.get("/v1/admin/billing/plans"), []);
  const overdue = useLoad(() => api.get("/v1/admin/billing/overdue"), [], 10000);
  const [f, setF] = useState({ code: "", name: "", currency: "EUR", monthly_fee: "", included_emails: 0, email_overage_price: "0" });
  const [planId, setPlanId] = useState("");
  const a = useAction(), b = useAction(), c = useAction();
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  return (
    <>
      <Card title="Plans (staff)" actions={<Button busy={c.busy} onClick={async () => { const r = await c.run(() => api.post("/v1/admin/billing/run"), "Billing run finished"); if (r) { alert(`${r.issued} invoice(s) issued`); reloadAll(); } }}>Run billing now</Button>}>
        <Table rows={plans.data || []} empty="No plans." cols={[{ label: "Code", key: "code" }, { label: "Name", key: "name" }, { label: "Fee", render: (r) => `${money(r.monthly_fee)} ${r.currency}` }, { label: "Included emails", key: "included_emails", num: true }, { label: "Overage / email", render: (r) => r.email_overage_price }, { label: "Status", render: (r) => <Badge>{r.status}</Badge> }]} />
        <div className="grid two" style={{ marginTop: 14 }}>
          <div className="form">
            <div className="grid two">
              <Field label="Code"><input value={f.code} onChange={set("code")} placeholder="growth" /></Field><Field label="Name"><input value={f.name} onChange={set("name")} /></Field>
              <Field label="Monthly fee"><input value={f.monthly_fee} onChange={set("monthly_fee")} /></Field><Field label="Currency"><input maxLength={3} value={f.currency} onChange={set("currency")} /></Field>
              <Field label="Included emails"><input type="number" value={f.included_emails} onChange={set("included_emails")} /></Field><Field label="Overage per email"><input value={f.email_overage_price} onChange={set("email_overage_price")} /></Field>
            </div>
            <Notice error={a.error} ok={a.ok} />
            <Button busy={a.busy} onClick={async () => { await a.run(() => api.post("/v1/admin/billing/plans", { ...f, included_emails: Number(f.included_emails) }), "Plan created"); plans.reload(); }}>Create plan</Button>
          </div>
          <div className="form">
            <Field label="Assign plan to the selected account"><select value={planId} onChange={(e) => setPlanId(e.target.value)}><option value="">Choose…</option>{(plans.data || []).filter((p) => p.status === "active").map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></Field>
            <Notice error={b.error} ok={b.ok} />
            <div className="row"><Button busy={b.busy} disabled={!planId} onClick={async () => { await b.run(() => api.put(`/v1/admin/billing/${api.owner()}/subscription`, { plan_id: Number(planId) }), "Plan assigned"); reloadAll(); }}>Assign plan</Button></div>
          </div>
        </div>
      </Card>
      <Card title="Overdue invoices"><Table rows={overdue.data || []} empty="Nothing overdue." cols={[{ label: "Account", key: "owner_ref" }, { label: "Number", key: "number" }, { label: "Total", render: (r) => `${money(r.total)} ${r.currency}` }, { label: "Due", render: (r) => when(r.due_at) }]} /></Card>
    </>
  );
}

export default function Billing({ me }) {
  const sub = useLoad(() => api.get("/v1/billing/subscription"), []);
  const prof = useLoad(() => api.get("/v1/billing/profile"), []);
  const ov = useLoad(() => api.get("/v1/portal/overview").catch(() => null), []);
  const pays = useLoad(() => api.get("/v1/billing/payments"), [], 5000);
  const reloadAll = () => { sub.reload(); prof.reload(); ov.reload(); pays.reload(); };
  const staff = me.permissions.includes("billing:admin") || me.permissions.includes("*");
  return (
    <>
      <Subscription sub={sub.data} />
      <Invoices reloadAll={reloadAll} />
      <TopUp wallets={ov.data?.wallets} reloadAll={reloadAll} />
      {!prof.loading && <Profile key={JSON.stringify(prof.data)} profile={prof.data} reload={reloadAll} />}
      <Card title="Payments">
        <Table rows={pays.data || []} empty="No online payments yet." cols={[{ label: "Purpose", key: "purpose" }, { label: "Amount", render: (r) => `${money(r.amount)} ${r.currency}` }, { label: "Status", render: (r) => <Badge>{r.status}</Badge> }, { label: "Note", render: (r) => r.failure_reason || "" }, { label: "Created", render: (r) => when(r.created_at) }]} />
      </Card>
      {staff && <Staff reloadAll={reloadAll} />}
    </>
  );
}
