import { useState } from "react";
import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { Badge, Button, Card, ErrorBox, Field, Stat, Table, Tabs, Time, money, useAction, useLoad } from "../ui.jsx";

const TYPE = { topup: T("Top-up"), hold: T("Reserved for a message"), capture: T("Charged"), release: T("Returned (not sent)"), refund: T("Refunded"), adjustment: T("Correction by us"), invoice: T("Invoice payment") };

const Delta = ({ v }) => {
  const n = Number(v);
  if (!n) return <span className="muted">-</span>;
  return <b style={{ color: n > 0 ? "var(--good)" : "var(--bad)" }}>{n > 0 ? "+" : ""}{money(n)}</b>;
};

function Ledger({ wallet }) {
  const led = useLoad(() => api.get(`/v1/wallets/${wallet.id}/ledger`, { limit: 500 }), [wallet.id], 8000);
  const items = [...(led.data || [])].reverse();
  return (
    <Table rows={items} loading={led.loading} emptyTitle={t("No movements yet")} empty={t("Top-ups and charges show up here, newest first.")}
      cols={[{ label: t("What happened"), render: (r) => (TYPE[r.entry_type] ? t(TYPE[r.entry_type]) : r.entry_type) },
        { label: t("Available"), num: true, render: (r) => <Delta v={r.available_delta} /> }, { label: t("Reserved"), num: true, render: (r) => <Delta v={r.held_delta} /> },
        { label: t("Balance after"), num: true, render: (r) => money(r.available_after) }, { label: t("Ref"), render: (r) => <small className="muted">{r.ref_id ? String(r.ref_id).slice(0, 12) : ""}</small> }]} />
  );
}

function TopUp({ wallet, onDone }) {
  const [amount, setAmount] = useState("25");
  const [link, setLink] = useState(null);
  const a = useAction();
  const bad = !/^\d+(\.\d{1,2})?$/.test(amount) || Number(amount) < 1;
  return (
    <Card title={t("Add money")} subtitle={t("You're redirected to a secure payment page. Your balance updates automatically when the payment is confirmed.")}>
      <div className="row wrap">
        {["10", "25", "50", "100"].map((v) => <Button key={v} className={amount === v ? "primary" : ""} onClick={() => setAmount(v)}>{v} {wallet.currency}</Button>)}
        <input aria-label={t("Amount")} style={{ maxWidth: 140 }} inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />
        <Button variant="primary" busy={a.busy} disabled={bad} onClick={async () => { const p = await a.run(() => api.post("/v1/billing/payments", { purpose: "topup", wallet_id: wallet.id, amount }), t("Payment page ready")); if (p && p.checkout_url) { setLink(p.checkout_url); onDone(); } }}>{t("Continue to payment")}</Button>
      </div>
      <ErrorBox error={a.error} />
      {link && <div className="alert good"><span>{t("Complete your payment here:")} <a href={link} target="_blank" rel="noopener noreferrer">{t("open payment page ↗")}</a></span></div>}
      <small className="muted">{t("Prefer a bank transfer or cash? Contact us and we'll credit your wallet once it arrives.")}</small>
    </Card>
  );
}

function LowBalance({ wallet, onDone }) {
  const [v, setV] = useState(wallet.low_balance_threshold ? String(Number(wallet.low_balance_threshold)) : "");
  const a = useAction();
  const bad = v !== "" && !/^\d+(\.\d{1,6})?$/.test(v);
  return (
    <Card title={t("Low-balance alert")} subtitle={t("We send a webhook event (wallet.low_balance) once when your balance drops below this amount.")}>
      <div className="row wrap">
        <Field label={t("Alert below ({currency})", { currency: wallet.currency })} hint={t("Leave empty to turn off")} error={bad ? t("Enter a number, e.g. 5 or 12.50") : null}>
          <input inputMode="decimal" value={v} onChange={(e) => setV(e.target.value)} placeholder="5.00" />
        </Field>
        <Button variant="primary" busy={a.busy} disabled={bad} onClick={async () => { await a.run(() => api.put(`/v1/wallets/${wallet.id}/alert`, { threshold: v === "" ? null : v }), t("Alert saved")); onDone(); }}>{t("Save")}</Button>
      </div>
      {wallet.low_balance && <div className="alert warn"><span>{t("Your balance is below the alert level. Top up to keep sending.")}</span></div>}
      <ErrorBox error={a.error} />
    </Card>
  );
}

export default function Wallet() {
  const wallets = useLoad(() => api.get("/v1/wallets"), [], 8000);
  const [idx, setIdx] = useState(0);
  const w = wallets.data?.[idx];
  const tops = useLoad(() => (w ? api.get(`/v1/wallets/${w.id}/topups`) : Promise.resolve([])), [w?.id], 8000);
  const pays = useLoad(() => api.get("/v1/billing/payments").catch(() => []), [], 8000);
  if (wallets.data && wallets.data.length === 0)
    return <Card title={t("No wallet yet")}><div className="empty"><b>{t("You don't have a wallet yet")}</b><div>{t("Contact us and we'll set one up so you can send SMS.")}</div></div></Card>;
  return (
    <>
      <ErrorBox error={wallets.error} retry={wallets.reload} />
      {wallets.data?.length > 1 && <Tabs value={String(idx)} onChange={(v) => setIdx(Number(v))} tabs={wallets.data.map((x, i) => ({ id: String(i), label: t("{currency} wallet", { currency: x.currency }) }))} />}
      {w && (
        <>
          <div className="grid stats">
            <Stat label={t("Available to spend")} value={`${money(w.available)} ${w.currency}`} tone={Number(w.available) < 5 ? "warn" : "good"} sub={Number(w.available) < 5 ? t("Running low") : undefined} />
            <Stat label={t("Reserved")} value={`${money(w.held)} ${w.currency}`} sub={t("Held for messages being sent. Returned if they fail.")} />
          </div>
          <LowBalance key={`${w.id}:${w.low_balance_threshold}`} wallet={w} onDone={wallets.reload} />
          <TopUp wallet={w} onDone={() => { pays.reload(); tops.reload(); }} />
          <Card title={t("Movements")} subtitle={t("Every change to your balance, newest first. Nothing on this list can be edited or deleted.")}><Ledger wallet={w} /></Card>
          <div className="grid two">
            <Card title={t("Top-ups")}><Table rows={tops.data || []} loading={tops.loading} empty={t("No top-ups yet.")} cols={[{ label: t("Amount"), render: (r) => `${money(r.amount)} ${w.currency}` }, { label: t("How"), render: (r) => t(r.method) }, { label: t("Status"), render: (r) => <Badge>{r.status}</Badge> }, { label: t("When"), render: (r) => <Time value={r.created_at} /> }]} /></Card>
            <Card title={t("Online payments")}><Table rows={pays.data || []} loading={pays.loading} empty={t("No online payments yet.")} cols={[{ label: t("For"), render: (r) => t(r.purpose) }, { label: t("Amount"), render: (r) => `${money(r.amount)} ${r.currency}` }, { label: t("Status"), render: (r) => <Badge>{r.status}</Badge> }, { label: t("Note"), render: (r) => r.failure_reason || "" }]} /></Card>
          </div>
        </>
      )}
    </>
  );
}
