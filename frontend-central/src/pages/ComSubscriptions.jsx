import { useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { UUID, buildHash } from "../routes.js";
import { Button, Card, Empty, Skeleton, StatusBadge, Table, useLoad } from "../ui.jsx";
import { ErrorState, DateOnly, EntLink, KV, Money, Note, Panel, ServerList, TabBar, Ts, humanize, shortId } from "../commercial/components.jsx";

const SUB_STATUS = ["active", "cancelled"];
const PERIOD_STATUS = ["invoiced", "no_charge"];

const periodCols = [
  { label: t("Period"), num: true, key: "period_index" },
  { label: t("From"), render: (p) => <DateOnly value={p.period_start} /> },
  { label: t("To"), render: (p) => <DateOnly value={p.period_end} /> },
  { label: t("Status"), render: (p) => <StatusBadge>{p.status}</StatusBadge> },
  { label: t("Invoice"), render: (p) => (p.invoice_id ? <a className="mono" href={buildHash("com-invoices", [p.invoice_id])}>{shortId(p.invoice_id)}</a> : "-") },
  { label: t("Billed"), hideSm: true, render: (p) => <Ts value={p.billed_at} /> },
];

function PlanVersion({ id }) {
  const v = useLoad((signal) => api.get(`/admin/billing/plan-versions/${id}`, {}, { signal }), [id]);
  return (
    <Panel title={t("Plan version")} state={v}>
      {(d) => <KV rows={[[t("Version"), d.version], [t("Status"), <StatusBadge>{d.status}</StatusBadge>], [t("Monthly fee"), <Money value={d.monthly_fee} currency={d.currency} />], [t("Included emails"), d.included_emails], [t("Activated"), <Ts value={d.activated_at} />]]} />}
    </Panel>
  );
}

function SubscriptionDetail({ eid, query }) {
  const sub = useLoad((signal) => api.get(`/admin/billing/subscriptions/${eid}`, {}, { signal }), [eid]);
  const prof = useLoad((signal) => api.get(`/admin/billing/profiles/${eid}`, {}, { signal }), [eid]);
  if (!UUID.test(eid)) return null;
  const s = sub.data;
  return (
    <>
      <p><a href={buildHash("com-subs")}>← {t("All subscriptions")}</a></p>
      <Panel title={t("Subscription")} state={sub}>
        {(d) => (
          <KV rows={[
            [t("Enterprise"), <EntLink id={d.enterprise_id} />], [t("Status"), <StatusBadge>{d.display_status}</StatusBadge>],
            [t("Plan version"), <span className="mono">{shortId(d.plan_version_id)}</span>],
            [t("Pending plan version"), d.pending_plan_version_id ? shortId(d.pending_plan_version_id) : "-"],
            [t("Cancels at period end"), d.cancel_at_period_end ? t("Yes") : t("No")],
            [t("Anchor started"), <Ts value={d.anchor_started_at} />], [t("Next period index"), d.next_period_index],
            [t("Next period"), <><DateOnly value={d.next_period?.start} /> – <DateOnly value={d.next_period?.end} /></>],
            [t("Cancelled"), <Ts value={d.cancelled_at} />],
          ]} />
        )}
      </Panel>
      {s && <PlanVersion id={s.plan_version_id} />}
      <Card title={t("Billing profile")} subtitle={t("The legal details printed on invoices.")}>
        {prof.error && prof.error.kind !== "not_found" ? <ErrorState error={prof.error} retry={prof.reload} />
          : prof.error ? <Empty title={t("No billing profile")}>{t("This enterprise has no billing profile yet.")}</Empty>
            : !prof.data ? <Skeleton rows={3} /> : (
              <KV rows={[[t("Legal name"), prof.data.legal_name], [t("Address"), prof.data.address], [t("Country"), prof.data.country], [t("Tax ID"), prof.data.tax_id || "-"], [t("Email"), prof.data.email], [t("VAT rate"), prof.data.vat_rate], [t("Updated"), <Ts value={prof.data.updated_at} />]]} />
            )}
      </Card>
      <ServerList route="com-subs" rest={[eid]} query={query} path="/admin/billing/periods" params={{ enterprise_id: eid }} title={t("Billing periods")} emptyTitle={t("No billing periods yet")} emptyText={t("Periods appear as each billing period is processed.")}
        filters={[{ name: "status", label: t("Status"), options: PERIOD_STATUS }]} cols={periodCols} />
    </>
  );
}

function PlanList({ query }) {
  const [open, setOpen] = useState(null);
  const plan = useLoad((signal) => (open ? api.get(`/admin/billing/plans/${open}`, {}, { signal }) : null), [open]);
  return (
    <>
      <ServerList route="com-subs" tab="plans" query={query} path="/admin/billing/plans" title={t("Plans")} emptyTitle={t("No plans yet")} emptyText={t("Plans appear once they are created.")}
        cols={[
          { label: t("Code"), render: (p) => <b>{p.code}</b> }, { label: t("Name"), key: "name" }, { label: t("Created"), hideSm: true, render: (p) => <Ts value={p.created_at} /> },
          { label: "", render: (p) => <Button className="small" aria-label={t("Versions of plan {code}", { code: p.code })} onClick={() => setOpen(p.id)}>{t("Versions")}</Button> },
        ]} />
      {open && (
        <Panel title={t("Plan versions")} state={plan}>
          {(d) => (
            <Table rows={d.versions} empty={t("No versions.")} cols={[
              { label: t("Version"), key: "version", num: true }, { label: t("Status"), render: (v) => <StatusBadge>{v.status}</StatusBadge> },
              { label: t("Monthly fee"), num: true, render: (v) => <Money value={v.monthly_fee} currency={v.currency} /> }, { label: t("Included emails"), key: "included_emails", num: true },
              { label: t("Activated"), hideSm: true, render: (v) => <Ts value={v.activated_at} /> },
            ]} />
          )}
        </Panel>
      )}
    </>
  );
}

export default function ComSubscriptions({ rest, query }) {
  const { active, bar } = TabBar({ route: "com-subs", query, tabs: [{ id: "subscriptions", label: t("Subscriptions") }, { id: "periods", label: t("Billing periods") }, { id: "plans", label: t("Plans") }] });
  if (rest.length) return <SubscriptionDetail eid={rest[0]} query={query} />;
  return (
    <>
      {bar}
      {active === "plans" ? <PlanList query={query} /> : active === "periods" ? (
        <ServerList route="com-subs" tab="periods" query={query} path="/admin/billing/periods" title={t("Billing periods")} emptyTitle={t("No billing periods yet")} emptyText={t("Periods appear as each billing period is processed.")}
          filters={[{ name: "enterprise_id", label: t("Enterprise ID") }, { name: "status", label: t("Status"), options: PERIOD_STATUS }]}
          cols={[{ label: t("Enterprise"), render: (p) => <a className="mono" href={buildHash("com-subs", [p.enterprise_id])}>{shortId(p.enterprise_id)}</a> }, ...periodCols]} />
      ) : (
        <ServerList route="com-subs" query={query} path="/admin/billing/subscriptions" title={t("Subscriptions")} emptyTitle={t("No subscriptions yet")} emptyText={t("Subscriptions appear once a plan is assigned to an enterprise.")}
          filters={[{ name: "status", label: t("Status"), options: SUB_STATUS }]}
          cols={[
            { label: t("Enterprise"), render: (s) => <a className="mono" href={buildHash("com-subs", [s.enterprise_id])}>{shortId(s.enterprise_id)}</a> },
            { label: t("Status"), render: (s) => <StatusBadge>{s.display_status}</StatusBadge> },
            { label: t("Plan version"), hideSm: true, render: (s) => <span className="mono">{shortId(s.plan_version_id)}</span> },
            { label: t("Next period"), render: (s) => <><DateOnly value={s.next_period?.start} /> – <DateOnly value={s.next_period?.end} /></> },
            { label: t("Period index"), hideSm: true, num: true, key: "next_period_index" },
            { label: "", render: (s) => <a className="btn small" href={buildHash("com-subs", [s.enterprise_id])} aria-label={t("Details for subscription {id}", { id: shortId(s.enterprise_id) })}>{t("Details")}</a> },
          ]} />
      )}
    </>
  );
}
