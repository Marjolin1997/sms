import { useState } from "react";
import { api, uuid } from "../api.js";
import { Badge, Button, Card, ErrorBox, Field, Table, Tabs, Time, money, useAction, useLoad, useUi } from "../ui.jsx";

function Pending() {
  const { confirm } = useUi();
  const tops = useLoad(() => api.get("/v1/topups", { status: "pending" }), [], 10000);
  const a = useAction();
  return (
    <Card title="Top-ups waiting for confirmation" subtitle="Confirm only when the money has really arrived. The person who created a top-up can't confirm it; a colleague must (except the owner account).">
      <ErrorBox error={a.error || tops.error} retry={tops.reload} />
      <Table rows={tops.data || []} loading={tops.loading} emptyTitle="Nothing waiting" empty="No pending top-ups." cols={[{ label: "Customer", render: (r) => <b>{r.owner_ref}</b> }, { label: "Amount", num: true, render: (r) => `${money(r.amount)} ${r.currency}` }, { label: "How", key: "method" }, { label: "Reference", render: (r) => r.external_ref || "-" }, { label: "Created by", render: (r) => r.created_by || "-" }, { label: "When", render: (r) => <Time value={r.created_at} /> },
        { label: "", render: (r) => <Button variant="primary" className="small" busy={a.busy} onClick={async () => { if (await confirm({ title: `Credit ${money(r.amount)} ${r.currency} to ${r.owner_ref}?`, body: "This adds money to their wallet immediately and is recorded permanently.", confirmLabel: "Confirm & credit" })) { await a.run(() => api.post(`/v1/topups/${r.id}/confirm`), "Wallet credited"); tops.reload(); } }}>Confirm</Button> }]} />
    </Card>
  );
}

function useWallets() {
  const accounts = useLoad(() => api.get("/v1/admin/accounts"), []);
  const options = (accounts.data || []).flatMap((a) => a.wallets.map((w) => ({ ...w, owner_ref: a.owner_ref, label: `${a.owner_ref} · ${w.currency} (balance ${money(w.available)})` })));
  return { options, loading: accounts.loading, error: accounts.error };
}

function NewTopup() {
  const w = useWallets();
  const [f, setF] = useState({ wallet: "", amount: "", method: "cash", ref: "" });
  const a = useAction();
  return (
    <Card title="Record a top-up" subtitle="For cash or bank transfers. It stays pending until a colleague confirms it.">
      <form className="form" onSubmit={async (e) => { e.preventDefault(); await a.run(() => api.post(`/v1/wallets/${f.wallet}/topups`, { amount: f.amount, method: f.method, external_ref: f.ref || null }), "Top-up recorded, waiting for confirmation"); setF({ ...f, amount: "", ref: "" }); }}>
        <ErrorBox error={w.error} />
        <Field label="Wallet"><select required value={f.wallet} onChange={(e) => setF({ ...f, wallet: e.target.value })}><option value="">Choose…</option>{w.options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}</select></Field>
        <div className="grid two">
          <Field label="Amount"><input required inputMode="decimal" value={f.amount} onChange={(e) => setF({ ...f, amount: e.target.value })} /></Field>
          <Field label="Received by"><select value={f.method} onChange={(e) => setF({ ...f, method: e.target.value })}><option value="cash">Cash</option><option value="electronic">Bank transfer</option></select></Field>
        </div>
        <Field label="Reference" hint="Bank transaction id or receipt number. The same reference can't be used twice."><input value={f.ref} onChange={(e) => setF({ ...f, ref: e.target.value })} /></Field>
        <ErrorBox error={a.error} />
        <div><Button variant="primary" busy={a.busy} disabled={!f.wallet || !f.amount}>Record top-up</Button></div>
      </form>
    </Card>
  );
}

function Correction() {
  const { confirm } = useUi();
  const w = useWallets();
  const [f, setF] = useState({ wallet: "", delta: "", note: "" });
  const a = useAction();
  const o = w.options.find((x) => String(x.id) === f.wallet);
  return (
    <>
      <div className="alert warn"><span><b>Use with care.</b> A correction changes a customer's balance directly. It's recorded with your name and the note, and can't be removed. Undo a mistake with an opposite correction.</span></div>
      <Card title="Balance correction">
        <form className="form" onSubmit={async (e) => {
          e.preventDefault();
          const d = Number(f.delta);
          if (!(await confirm({ title: `${d > 0 ? "Add" : "Remove"} ${money(Math.abs(d))} ${o?.currency || ""} ${d > 0 ? "to" : "from"} ${o?.owner_ref}?`, body: `Reason on record: “${f.note}”`, danger: d < 0, confirmLabel: "Apply correction" }))) return;
          await a.run(() => api.post(`/v1/wallets/${f.wallet}/adjustments`, { delta: f.delta, key: uuid(), note: f.note }), "Correction applied");
          setF({ wallet: "", delta: "", note: "" });
        }}>
          <Field label="Wallet"><select required value={f.wallet} onChange={(e) => setF({ ...f, wallet: e.target.value })}><option value="">Choose…</option>{w.options.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}</select></Field>
          <Field label="Change" hint="Positive adds money, negative removes. A balance can't go below zero."><input required inputMode="decimal" placeholder="e.g. 5.00 or -2.50" value={f.delta} onChange={(e) => setF({ ...f, delta: e.target.value })} /></Field>
          <Field label="Reason" hint="Shown in the audit log. Be specific."><input required minLength={3} value={f.note} onChange={(e) => setF({ ...f, note: e.target.value })} placeholder="Goodwill credit for outage on 12 Sep" /></Field>
          <ErrorBox error={a.error} />
          <div><Button variant="primary" busy={a.busy} disabled={!f.wallet || !/^-?\d+(\.\d{1,6})?$/.test(f.delta) || Number(f.delta) === 0 || f.note.trim().length < 3}>Review correction…</Button></div>
        </form>
      </Card>
    </>
  );
}

export default function Finance({ me }) {
  const [tab, setTab] = useState("pending");
  const canAdjust = me.permissions.includes("*") || me.permissions.includes("wallet:adjust");
  return (
    <>
      <Tabs value={tab} onChange={setTab} tabs={[{ id: "pending", label: "Pending top-ups" }, { id: "new", label: "Record top-up" }, ...(canAdjust ? [{ id: "fix", label: "Correction" }] : [])]} />
      {tab === "pending" && <Pending />}{tab === "new" && <NewTopup />}{tab === "fix" && <Correction />}
    </>
  );
}
