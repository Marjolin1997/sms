import { useState } from "react";
import { api, uuid } from "../api.js";
import { Badge, Button, Card, ErrorBox, Field, SecretBanner, Stat, Table, useAction, useLoad, useUi, money } from "../ui.jsx";

function Manage({ acc, cards, plans, reload, pick }) {
  const { confirm } = useUi();
  const [rc, setRc] = useState(acc.rate_card_id || "");
  const [limit, setLimit] = useState(acc.rate_limit_per_min || "");
  const [cur, setCur] = useState("EUR");
  const [planId, setPlanId] = useState("");
  const [vat, setVat] = useState("");
  const [key, setKey] = useState(null);
  const a = useAction(), b = useAction(), c = useAction(), d = useAction(), e = useAction();
  return (
    <>
      {key && <SecretBanner title="API key for this customer" note="Send it to them through a secure channel. It's shown only once." value={key} onClose={() => setKey(null)} />}
      <Card title={acc.owner_ref} actions={<Button variant="primary" onClick={() => pick(acc.owner_ref)}>Open as this account →</Button>}>
        <div className="grid stats">
          <Stat label="Sending" value={acc.sending_enabled ? "Enabled" : "Off"} tone={acc.sending_enabled ? "good" : "bad"} />
          {acc.wallets.map((w) => <Stat key={w.id} label={`Wallet ${w.currency}`} value={money(w.available)} />)}
          <Stat label="Subscription" value={acc.subscription || "none"} />
        </div>
      </Card>
      <div className="grid two">
        <Card title="Sending" subtitle="Which price list they use, and whether they can send at all">
          <div className="form">
            <Field label="Price list"><select value={rc} onChange={(x) => setRc(x.target.value)}><option value="">Choose…</option>{cards.map((x) => <option key={x.id} value={x.id}>{x.name} ({x.currency})</option>)}</select></Field>
            <Field label="Messages per minute limit" hint="Leave empty for the default (600)"><input type="number" min="1" value={limit} onChange={(x) => setLimit(x.target.value)} /></Field>
            <ErrorBox error={a.error} />
            <div className="row"><Button variant="primary" busy={a.busy} disabled={!rc} onClick={async () => { await a.run(() => api.put(`/v1/admin/plans/${acc.owner_ref}`, { rate_card_id: Number(rc), enabled: true, rate_limit_per_min: limit ? Number(limit) : null }), "Sending enabled"); reload(); }}>{acc.has_rate_card ? "Save & enable" : "Enable sending"}</Button>
              {acc.has_rate_card && acc.sending_enabled && <Button variant="danger" busy={a.busy} onClick={async () => { if (await confirm({ title: `Stop ${acc.owner_ref} from sending?`, body: "New messages are rejected until you enable sending again. Messages already queued still go out.", danger: true, confirmLabel: "Stop sending" })) { await a.run(() => api.put(`/v1/admin/plans/${acc.owner_ref}`, { rate_card_id: acc.rate_card_id, enabled: false, rate_limit_per_min: acc.rate_limit_per_min }), "Sending stopped"); reload(); } }}>Stop sending</Button>}</div>
          </div>
        </Card>
        <Card title="Wallet" subtitle="One wallet per currency">
          <div className="form">
            {acc.wallets.length > 0 && <small className="muted">Has: {acc.wallets.map((w) => w.currency).join(", ")}</small>}
            <Field label="Create a wallet in currency"><input maxLength={3} value={cur} onChange={(x) => setCur(x.target.value.toUpperCase())} /></Field>
            <ErrorBox error={b.error} />
            <div><Button busy={b.busy} disabled={cur.length !== 3} onClick={async () => { await b.run(() => api.post("/v1/wallets", { owner_ref: acc.owner_ref, currency: cur }), "Wallet ready"); reload(); }}>Create wallet</Button></div>
            <small className="muted">To add money, go to Finance.</small>
          </div>
        </Card>
        <Card title="Plan & tax" subtitle="Needs the customer's billing details first">
          <div className="form">
            <Field label="Subscription plan"><select value={planId} onChange={(x) => setPlanId(x.target.value)}><option value="">Choose…</option>{plans.filter((p) => p.status === "active").map((p) => <option key={p.id} value={p.id}>{p.name} ({money(p.monthly_fee)} {p.currency})</option>)}</select></Field>
            <ErrorBox error={c.error} />
            <div><Button busy={c.busy} disabled={!planId} onClick={async () => { await c.run(() => api.put(`/v1/admin/billing/${acc.owner_ref}/subscription`, { plan_id: Number(planId) }), "Plan assigned (applies from the next period if they already had one)"); reload(); }}>Assign plan</Button></div>
            <Field label="VAT rate" hint="A number from 0 to 1, e.g. 0.2 for 20%"><input inputMode="decimal" value={vat} onChange={(x) => setVat(x.target.value)} /></Field>
            <ErrorBox error={d.error} />
            <div><Button busy={d.busy} disabled={vat === ""} onClick={() => d.run(() => api.put(`/v1/admin/billing/${acc.owner_ref}/vat`, { vat_rate: vat }), "VAT rate saved")}>Save VAT</Button></div>
          </div>
        </Card>
        <Card title="Access" subtitle="Give the customer a key to sign in to this console">
          <ErrorBox error={e.error} />
          <Button busy={e.busy} onClick={async () => { const r = await e.run(() => api.post("/v1/admin/api-keys", { name: "Console access", role: "client", owner_ref: acc.owner_ref }), "Key created"); if (r && r.key) setKey(r.key); }}>Create a key for {acc.owner_ref}</Button>
        </Card>
      </div>
    </>
  );
}

export default function Accounts({ pick }) {
  const accounts = useLoad(() => api.get("/v1/admin/accounts"), [], 15000);
  const cards = useLoad(() => api.get("/v1/rate-cards").catch(() => []), []);
  const plans = useLoad(() => api.get("/v1/admin/billing/plans").catch(() => []), []);
  const [sel, setSel] = useState(null);
  const [name, setName] = useState("");
  const [f, setF] = useState(null);
  const acc = (accounts.data || []).find((x) => x.owner_ref === sel) || (f ? { owner_ref: f, wallets: [], sending_enabled: false, has_rate_card: false } : null);
  if (acc) return (<><Button onClick={() => { setSel(null); setF(null); }}>← All accounts</Button><Manage acc={acc} cards={cards.data || []} plans={plans.data || []} reload={accounts.reload} pick={pick} /></>);
  const rows = (accounts.data || []).map((x) => ({ ...x, id: x.owner_ref, _onClick: () => setSel(x.owner_ref) }));
  return (
    <>
      <Card title="Customer accounts">
        <ErrorBox error={accounts.error} retry={accounts.reload} />
        <Table rows={rows} loading={accounts.loading} emptyTitle="No accounts yet" empty="Create the first one below." cols={[{ label: "Account", render: (r) => <b>{r.owner_ref}</b> }, { label: "Sending", render: (r) => <Badge>{r.sending_enabled ? "active" : "disabled"}</Badge> }, { label: "Wallets", render: (r) => r.wallets.map((w) => `${money(w.available)} ${w.currency}`).join(" · ") || "none" }, { label: "Plan", render: (r) => r.subscription || "-" }, { label: "SMS sent", num: true, key: "sms_last_24h" }]} />
      </Card>
      <Card title="Create an account" subtitle="An account is a customer. Use a short lowercase name; it can't be renamed.">
        <div className="row wrap"><input aria-label="Account name" style={{ maxWidth: 260 }} placeholder="e.g. acme" value={name} onChange={(e) => setName(e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, ""))} /><Button variant="primary" disabled={name.length < 2} onClick={() => setF(name)}>Continue</Button></div>
      </Card>
    </>
  );
}
