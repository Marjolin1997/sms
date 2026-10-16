import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { buildHash } from "../routes.js";
import { MetricCard, Skeleton, Table, useLoad } from "../ui.jsx";
import { ErrorState, Level, Money, POLL_MS, Panel, EntLink, Ts, humanize, shortId } from "../commercial/components.jsx";
import { level } from "../commercial/format.js";

const Summary = ({ href, label, value, sub, tone }) => (
  <a className="summary-card" href={href} aria-label={label}><MetricCard label={label} value={value} sub={sub} tone={tone} /></a>
);

export default function ComOverview() {
  const fin = useLoad((signal) => api.get("/admin/financial/overview", {}, { signal }), [], POLL_MS);
  const ops = useLoad((signal) => api.get("/admin/billing/ops", {}, { signal }), [], POLL_MS);
  if (fin.error && !fin.data && ops.error && !ops.data) return <ErrorState error={fin.error} retry={() => { fin.reload(); ops.reload(); }} />;
  const f = fin.data, o = ops.data;
  return (
    <>
      <div className="grid three">
        <Summary href={buildHash("com-recon")} label={t("Reconciliation")} value={f ? <Level value={f.reconciliation?.status} /> : <Skeleton rows={1} />}
          sub={f ? t("Warnings: {n}", { n: f.reconciliation?.counts?.WARN ?? 0 }) : null} />
        <Summary href={buildHash("com-credit")} label={t("Credit accounts")} value={f ? f.accounts.length : "-"} sub={f ? t("Active grants: {n}", { n: f.grants.active }) : null} />
        <Summary href={buildHash("com-payments")} label={t("Pending payments")} value={f ? f.payments.pending : "-"} sub={f ? t("Stale: {n}", { n: f.payments.stale_pending }) : null} tone={f?.payments.stale_pending ? "warn" : ""} />
        <Summary href={buildHash("com-invoices")} label={t("Overdue open invoices")} value={o ? o.settlement.invoices_overdue_open : "-"} sub={o ? t("Pending invoice payments: {n}", { n: o.settlement.invoice_payments_pending }) : null} tone={o?.settlement.invoices_overdue_open ? "bad" : ""} />
        <Summary href={buildHash("com-subs", [], { tab: "periods" })} label={t("Periods awaiting processing")} value={o ? o.periods.due_unprocessed : "-"} sub={o ? t("Waiting for usage: {n}", { n: o.usage.periods_waiting_for_usage }) : null} />
        <Summary href={buildHash("com-ready")} label={t("Billing authority")} value={o ? humanize(o.authority.mode) : "-"} sub={o ? t("Central invoices: {n}", { n: o.authority.central_invoices }) : null} />
      </div>
      <Panel title={t("Credit accounts")} subtitle={t("Figures are computed by the server from the ledger.")} state={fin}>
        {(d) => (
          <Table rows={d.accounts.map((a) => ({ ...a, id: a.account_id }))} empty={t("No credit accounts yet.")} cols={[
            { label: t("Account"), render: (a) => <a className="mono" href={buildHash("com-credit", [a.account_id])}>{shortId(a.account_id)}</a> },
            { label: t("Enterprise"), render: (a) => <EntLink id={a.enterprise_id} /> },
            { label: t("Status"), render: (a) => <span>{humanize(a.status)}</span> },
            { label: t("Funds"), num: true, render: (a) => <Money value={a.funds} currency={a.currency} /> },
            { label: t("Outstanding grants"), num: true, hideSm: true, render: (a) => <Money value={a.outstanding_grants} currency={a.currency} /> },
            { label: t("Available to grant"), num: true, render: (a) => <Money value={a.available_to_grant} currency={a.currency} /> },
          ]} />
        )}
      </Panel>
      <Panel title={t("Financial alerts")} state={fin} subtitle={f ? <>{t("Generated")}: <Ts value={f.generated_at} /></> : null}>
        {(d) => (
          <Table rows={d.alerts.map((a, i) => ({ ...a, id: `${a.code}-${i}` }))} empty={t("No active financial alerts.")} cols={[
            { label: t("Severity"), render: (a) => <Level value={a.level} /> },
            { label: t("Alert"), render: (a) => <b>{humanize(a.code)}</b> },
            { label: t("Enterprise"), hideSm: true, render: (a) => <EntLink id={a.enterprise_id} /> },
            { label: t("Detail"), key: "message" },
          ]} />
        )}
      </Panel>
      <Panel title={t("Billing worker")} state={ops}>
        {(d) => (
          <dl className="kvlist">
            <dt>{t("Last billing run")}</dt><dd><Ts value={d.worker.last_run_at} /></dd>
            <dt>{t("Invoices issued last run")}</dt><dd>{d.worker.invoices_issued_last_run ?? "-"}</dd>
            <dt>{t("Last run failed")}</dt><dd>{d.worker.last_run_failed == null ? "-" : String(d.worker.last_run_failed)}</dd>
            <dt>{t("Legacy billing frozen (attested)")}</dt><dd>{d.authority.legacy_worker_frozen_attested ? t("Yes") : t("No")}</dd>
          </dl>
        )}
      </Panel>
    </>
  );
}
