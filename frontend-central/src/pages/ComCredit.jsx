import { useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { UUID, buildHash } from "../routes.js";
import { Button, Card, Skeleton, Table, useLoad } from "../ui.jsx";
import { ErrorState, Actor, DetailModal, EntLink, KV, Money, Note, Panel, ServerList, Ts, DateOnly, TabBar, humanize, shortId } from "../commercial/components.jsx";
import { StatusBadge } from "../ui.jsx";

const grantRows = (g) => [
  [t("Grant"), g.id], [t("Account"), g.account_id], [t("Enterprise"), g.enterprise_id], [t("Amount"), <Money value={g.amount} currency={g.currency} />],
  [t("Status"), <StatusBadge>{g.status}</StatusBadge>], [t("Purpose"), humanize(g.purpose)], [t("Source payment"), g.source_payment_id || "-"], [t("Note"), g.note || "-"],
  [t("Created by"), <Actor a={g.created_by} />], [t("Created"), <Ts value={g.created_at} />],
  [t("Reversed by"), <Actor a={g.reversed_by} />], [t("Reversed"), <Ts value={g.reversed_at} />], [t("Reversal reason"), g.reversal_reason || "-"],
];

function GrantsTable({ route, rest, tab, query, fixed }) {
  const [open, setOpen] = useState(null);
  return (
    <>
      <ServerList route={route} rest={rest} tab={tab} query={query} path="/admin/money/grants" params={fixed} title={t("Credit grants")} emptyTitle={t("No grants yet")} emptyText={t("Grants appear here once credit has been granted to an account.")}
        filters={fixed ? [{ name: "status", label: t("Status"), options: ["active", "reversed"] }] : [{ name: "account_id", label: t("Account ID") }, { name: "status", label: t("Status"), options: ["active", "reversed"] }]}
        cols={[
          { label: t("Grant"), render: (g) => <span className="mono">{shortId(g.id)}</span> },
          { label: t("Enterprise"), render: (g) => <EntLink id={g.enterprise_id} /> },
          { label: t("Amount"), num: true, render: (g) => <Money value={g.amount} currency={g.currency} /> },
          { label: t("Status"), render: (g) => <StatusBadge>{g.status}</StatusBadge> },
          { label: t("Purpose"), hideSm: true, render: (g) => humanize(g.purpose) },
          { label: t("Created"), hideSm: true, render: (g) => <Ts value={g.created_at} /> },
          { label: "", render: (g) => <Button className="small" aria-label={t("Details for grant {id}", { id: shortId(g.id) })} onClick={() => setOpen(g)}>{t("Details")}</Button> },
        ]} />
      {open && <DetailModal title={t("Grant")} rows={grantRows(open)} onClose={() => setOpen(null)} />}
    </>
  );
}

function Totals({ totals, currency }) {
  const m = (v) => <Money value={v} currency={currency} />;
  return (
    <KV rows={[
      [t("Funds"), m(totals.funds)], [t("Outstanding grants"), m(totals.outstanding_grants)], [t("Available to grant"), m(totals.available_to_grant)],
      [t("Payment credits"), m(totals.payment_credits)], [t("Credit adjustments"), m(totals.credit_adjustments)], [t("Debit adjustments"), m(totals.debit_adjustments)],
      [t("Grants issued"), m(totals.grants_issued)], [t("Grants reversed"), m(totals.grants_reversed)], [t("Ledger entries"), totals.entries],
    ]} />
  );
}

function Ledger({ id, currency }) {
  const [after, setAfter] = useState(0);
  const [pages, setPages] = useState([]);
  const cur = useLoad(async (signal) => api.get(`/admin/money/accounts/${id}/ledger`, { after_seq: after, limit: 100 }, { signal }), [id, after]);
  const items = [...pages.flatMap((p) => p.items), ...(cur.data ? cur.data.items : [])].filter((e, i, a) => a.findIndex((x) => x.seq === e.seq) === i);
  const next = cur.data?.next_after_seq;
  return (
    <Card title={t("Ledger")} subtitle={t("Append-only history, oldest first. Nothing here can be edited.")}>
      {cur.error && !cur.data && pages.length === 0 ? <ErrorState error={cur.error} retry={cur.reload} /> : cur.loading && items.length === 0 ? <Skeleton rows={4} /> : (
        <>
          <Table rows={items.map((e) => ({ ...e, id: e.seq }))} empty={t("No ledger entries yet.")} cols={[
            { label: "#", key: "seq" },
            { label: t("Type"), render: (e) => humanize(e.entry_type) },
            { label: t("Amount"), num: true, render: (e) => <Money value={e.amount} currency={e.currency || currency} /> },
            { label: t("Source"), hideSm: true, render: (e) => humanize(e.source_type) },
            { label: t("Reason"), hideSm: true, render: (e) => e.reason || "-" },
            { label: t("Actor"), hideSm: true, render: (e) => <Actor a={e.actor} /> },
            { label: t("When"), render: (e) => <Ts value={e.created_at} /> },
          ]} />
          {cur.error && <ErrorState stale error={cur.error} retry={cur.reload} />}
          {next != null && <div className="row end"><Button disabled={cur.loading} onClick={() => { setPages((p) => [...p, cur.data]); setAfter(next); }}>{t("Load more")}</Button></div>}
        </>
      )}
    </Card>
  );
}

function AccountDetail({ id, query }) {
  const acc = useLoad((signal) => api.get(`/admin/money/accounts/${id}`, {}, { signal }), [id]);
  const rep = useLoad(async (signal) => (acc.data ? api.get("/admin/money/usage-reports", { enterprise_id: acc.data.enterprise_id }, { signal }) : null), [acc.data?.enterprise_id]);
  if (!UUID.test(id)) return null;
  const a = acc.data;
  const mine = a && rep.data ? rep.data.items.filter((r) => r.product_id === a.product_id && r.currency === a.currency) : [];
  return (
    <>
      <p><a href={buildHash("com-credit")}>← {t("All credit accounts")}</a></p>
      <Panel title={t("Credit account")} state={acc}>
        {(d) => (
          <>
            <KV rows={[[t("Account"), d.id], [t("Enterprise"), <EntLink id={d.enterprise_id} />], [t("Product"), d.product_id], [t("Currency"), d.currency], [t("Status"), <StatusBadge>{d.status}</StatusBadge>], [t("Created"), <Ts value={d.created_at} />]]} />
            {d.totals && <Totals totals={d.totals} currency={d.currency} />}
            <Note>{t("Totals are computed by the server from the ledger. The console never recomputes them.")}</Note>
          </>
        )}
      </Panel>
      <Card title={t("Balance reported by Enterprise")} subtitle={t("The latest usage report from the Enterprise side, shown as received.")}>
        {rep.error ? <ErrorState error={rep.error} retry={rep.reload} /> : !rep.data ? <Skeleton rows={2} /> : mine.length === 0 ? <Note>{t("No usage report has been received for this account yet.")}</Note> : (
          mine.map((r) => <KV key={r.report_id} rows={[[t("Available"), <Money value={r.available} currency={r.currency} />], [t("Held"), <Money value={r.held} currency={r.currency} />], [t("Gross"), <Money value={r.gross} currency={r.currency} />], [t("Authority mode"), humanize(r.authority_mode)], [t("Received"), <Ts value={r.received_at} />]]} />)
        )}
      </Card>
      {a && <GrantsTable route="com-credit" rest={[id]} tab={null} query={query} fixed={{ account_id: id }} />}
      <Ledger id={id} currency={a?.currency} />
    </>
  );
}

function AccountList({ query }) {
  const { active, bar } = TabBar({ route: "com-credit", query, tabs: [{ id: "accounts", label: t("Credit accounts") }, { id: "grants", label: t("Grants") }] });
  return (
    <>
      {bar}
      {active === "grants" ? <GrantsTable route="com-credit" tab="grants" query={query} /> : (
        <ServerList route="com-credit" query={query} path="/admin/money/accounts" title={t("Credit accounts")} emptyTitle={t("No credit accounts yet")} emptyText={t("Accounts appear after the first Enterprise credit setup.")}
          filters={[{ name: "enterprise_id", label: t("Enterprise ID") }]}
          cols={[
            { label: t("Account"), render: (a) => <a className="mono" href={buildHash("com-credit", [a.id])}>{shortId(a.id)}</a> },
            { label: t("Enterprise"), render: (a) => <EntLink id={a.enterprise_id} /> },
            { label: t("Status"), render: (a) => <StatusBadge>{a.status}</StatusBadge> },
            { label: t("Funds"), num: true, render: (a) => <Money value={a.totals?.funds} currency={a.currency} /> },
            { label: t("Outstanding grants"), num: true, hideSm: true, render: (a) => <Money value={a.totals?.outstanding_grants} currency={a.currency} /> },
            { label: t("Available to grant"), num: true, render: (a) => <Money value={a.totals?.available_to_grant} currency={a.currency} /> },
            { label: "", render: (a) => <a className="btn small" href={buildHash("com-credit", [a.id])} aria-label={t("Details for account {id}", { id: shortId(a.id) })}>{t("Details")}</a> },
          ]} />
      )}
    </>
  );
}

export default function ComCredit({ rest, query }) {
  return rest.length ? <AccountDetail id={rest[0]} query={query} /> : <AccountList query={query} />;
}
