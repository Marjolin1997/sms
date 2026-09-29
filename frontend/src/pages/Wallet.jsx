import { useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, ErrorBox, Field, Stat, Table, Tabs, Time, money, useAction, useLoad } from "../ui.jsx";

const TYPE = { topup: "Top-up", hold: "Reserved for a message", capture: "Charged", release: "Returned (not sent)", refund: "Refunded", adjustment: "Correction by us", invoice: "Invoice payment" };

const Delta = ({ v }) => {
  const n = Number(v);
  if (!n) return <span className="muted">-</span>;
  return <b style={{ color: n > 0 ? "var(--good)" : "var(--bad)" }}>{n > 0 ? "+" : ""}{money(n)}</b>;
};

function Ledger({ wallet }) {
  const led = useLoad(() => api.get(`/v1/wallets/${wallet.id}/ledger`, { limit: 500 }), [wallet.id], 8000);
  const items = [...(led.data || [])].reverse();
  return (
    <Table rows={items} loading={led.loading} emptyTitle="No movements yet" empty="Top-ups and charges show up here, newest first."
      cols={[{ label: "What happened", render: (r) => TYPE[r.entry_type] || r.entry_type },
        { label: "Available", num: true, render: (r) => <Delta v={r.available_delta} /> }, { label: "Reserved", num: true, render: (r) => <Delta v={r.held_delta} /> },
        { label: "Balance after", num: true, render: (r) => money(r.available_after) }, { label: "Ref", render: (r) => <small className="muted">{r.ref_id ? String(r.ref_id).slice(0, 12) : ""}</small> }]} />
  );
}

function TopUp({ wallet, onDone }) {
  const [amount, setAmount] = useState("25");
  const [link, setLink] = useState(null);
  const a = useAction();
  const bad = !/^\d+(\.\d{1,2})?$/.test(amount) || Number(amount) < 1;
  return (
    <Card title="Add money" subtitle="You're redirected to a secure payment page. Your balance updates automatically when the payment is confirmed.">
      <div className="row wrap">
        {["10", "25", "50", "100"].map((v) => <Button key={v} className={amount === v ? "primary" : ""} onClick={() => setAmount(v)}>{v} {wallet.currency}</Button>)}
        <input aria-label="Amount" style={{ maxWidth: 140 }} inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />
        <Button variant="primary" busy={a.busy} disabled={bad} onClick={async () => { const p = await a.run(() => api.post("/v1/billing/payments", { purpose: "topup", wallet_id: wallet.id, amount }), "Payment page ready"); if (p && p.checkout_url) { setLink(p.checkout_url); onDone(); } }}>Continue to payment</Button>
      </div>
      <ErrorBox error={a.error} />
      {link && <div className="alert good"><span>Complete your payment here: <a href={link} target="_blank" rel="noopener noreferrer">open payment page ↗</a></span></div>}
      <small className="muted">Prefer a bank transfer or cash? Contact us and we'll credit your wallet once it arrives.</small>
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
    return <Card title="No wallet yet"><div className="empty"><b>You don't have a wallet yet</b><div>Contact us and we'll set one up so you can send SMS.</div></div></Card>;
  return (
    <>
      <ErrorBox error={wallets.error} retry={wallets.reload} />
      {wallets.data?.length > 1 && <Tabs value={String(idx)} onChange={(v) => setIdx(Number(v))} tabs={wallets.data.map((x, i) => ({ id: String(i), label: `${x.currency} wallet` }))} />}
      {w && (
        <>
          <div className="grid stats">
            <Stat label="Available to spend" value={`${money(w.available)} ${w.currency}`} tone={Number(w.available) < 5 ? "warn" : "good"} sub={Number(w.available) < 5 ? "Running low" : undefined} />
            <Stat label="Reserved" value={`${money(w.held)} ${w.currency}`} sub="Held for messages being sent. Returned if they fail." />
          </div>
          <TopUp wallet={w} onDone={() => { pays.reload(); tops.reload(); }} />
          <Card title="Movements" subtitle="Every change to your balance, newest first. Nothing on this list can be edited or deleted."><Ledger wallet={w} /></Card>
          <div className="grid two">
            <Card title="Top-ups"><Table rows={tops.data || []} loading={tops.loading} empty="No top-ups yet." cols={[{ label: "Amount", render: (r) => `${money(r.amount)} ${w.currency}` }, { label: "How", key: "method" }, { label: "Status", render: (r) => <Badge>{r.status}</Badge> }, { label: "When", render: (r) => <Time value={r.created_at} /> }]} /></Card>
            <Card title="Online payments"><Table rows={pays.data || []} loading={pays.loading} empty="No online payments yet." cols={[{ label: "For", key: "purpose" }, { label: "Amount", render: (r) => `${money(r.amount)} ${r.currency}` }, { label: "Status", render: (r) => <Badge>{r.status}</Badge> }, { label: "Note", render: (r) => r.failure_reason || "" }]} /></Card>
          </div>
        </>
      )}
    </>
  );
}
