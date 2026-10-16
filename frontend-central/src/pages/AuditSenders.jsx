import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { Badge, Button, Card, Empty, ErrorState, Pagination, Skeleton, StatusBadge, Table, Time, useLoad } from "../ui.jsx";
import { DateFilter, ExportButton, Filters, goTo } from "../audit/components.jsx";
import { actorText, decisionLabel } from "../governance/components.jsx";
import { buildHash } from "../routes.js";

const SIZES = [25, 50, 100, 200];
const DECISIONS = ["requested", "approved", "rejected", "revoked", "resubmitted"];
const CATEGORIES = { request: T("Request"), manual: T("Manual decision"), resubmit: T("Resubmitted"), policy_denied: T("Denied by policy"), policy_auto_approved: T("Approved by policy"), policy_revoked: T("Revoked by policy") };
const SOURCES = ["admin", "enterprise", "import"];
const ROUTE = "audit-senders";

export default function AuditSenders({ query = {} }) {
  const limit = SIZES.includes(Number(query.limit)) ? Number(query.limit) : 50;
  const offset = Math.max(0, Number.parseInt(query.offset, 10) || 0);
  const f = { decision: query.decision || "", category: query.category || "", source: query.source || "", country: (query.country || "").toUpperCase(), enterprise_id: query.enterprise_id || "", from: query.from || "", to: query.to || "" };
  const key = JSON.stringify({ limit, offset, ...f });
  const list = useLoad(async (signal) => ({ key, page: await api.page("/admin/audit/sender-decisions", { limit, offset, ...f }, { signal }) }), [key]);
  const page = list.data?.page, pending = list.data && list.data.key !== key;
  const set = (patch) => goTo(ROUTE, { ...f, limit: query.limit, ...patch, offset: "" });
  const filtering = Object.values(f).some(Boolean);
  return (
    <Card title={t("Sender decisions")} subtitle={t("Every approval, rejection, revocation and automatic policy decision, newest first. Days are UTC.")} actions={<ExportButton path="/admin/audit/sender-decisions.csv" params={f} file="sender-decisions.csv" />}>
      <Filters label={t("Filter sender decisions")}>
        <select aria-label={t("Decision")} value={f.decision} onChange={(e) => set({ decision: e.target.value })}><option value="">{t("All decisions")}</option>{DECISIONS.map((d) => <option key={d} value={d}>{decisionLabel(d)}</option>)}</select>
        <select aria-label={t("Category")} value={f.category} onChange={(e) => set({ category: e.target.value })}><option value="">{t("All categories")}</option>{Object.entries(CATEGORIES).map(([k, v]) => <option key={k} value={k}>{t(v)}</option>)}</select>
        <select aria-label={t("Source")} value={f.source} onChange={(e) => set({ source: e.target.value })}><option value="">{t("All sources")}</option>{SOURCES.map((s) => <option key={s} value={s}>{s}</option>)}</select>
        <input aria-label={t("Country")} placeholder={t("Country (ISO code)")} maxLength={2} defaultValue={f.country} onBlur={(e) => /^[A-Za-z]{0,2}$/.test(e.target.value) && e.target.value.toUpperCase() !== f.country && set({ country: e.target.value.toUpperCase() })} onKeyDown={(e) => e.key === "Enter" && /^[A-Za-z]{0,2}$/.test(e.target.value) && set({ country: e.target.value.toUpperCase() })} />
        <input aria-label={t("Enterprise ID")} placeholder={t("Enterprise ID")} defaultValue={f.enterprise_id} onBlur={(e) => e.target.value.trim() !== f.enterprise_id && set({ enterprise_id: e.target.value.trim() })} onKeyDown={(e) => e.key === "Enter" && set({ enterprise_id: e.target.value.trim() })} />
        <DateFilter from={f.from} to={f.to} onChange={(from, to) => set({ from, to })} />
        {filtering && <Button type="button" onClick={() => goTo(ROUTE, {})}>{t("Clear filters")}</Button>}
        <select aria-label={t("Rows per page")} value={limit} onChange={(e) => set({ limit: e.target.value === "50" ? "" : e.target.value })}>{SIZES.map((n) => <option key={n} value={n}>{t("{n} per page", { n })}</option>)}</select>
      </Filters>
      {list.error && !page ? <ErrorState error={list.error} retry={list.error.retryable ? list.reload : undefined} /> : !page ? <Skeleton rows={5} /> : (
        <div aria-busy={pending || undefined} style={{ opacity: pending ? 0.55 : 1 }}>
          {list.stale && <ErrorState stale error={list.error} retry={list.reload} />}
          {page.items.length === 0 ? (
            offset > 0 ? <Empty title={t("No more results")} action={<Button onClick={() => set({})}>{t("Back to first page")}</Button>}>{t("This page is past the end of the list.")}</Empty>
              : <Empty title={filtering ? t("No decisions match your filters") : t("No sender decisions yet")}>{filtering ? t("Try different filters.") : t("Decisions appear here when senders are requested or decided.")}</Empty>
          ) : (
            <Table rows={page.items} cols={[
              { label: t("When"), render: (d) => <Time value={d.decided_at} /> },
              { label: t("Sender"), render: (d) => <a href={buildHash("senders", [d.registry_id])}><b>{d.sender}</b></a> },
              { label: t("Enterprise"), hideSm: true, render: (d) => d.enterprise },
              { label: t("Country"), hideSm: true, key: "country" },
              { label: t("Decision"), render: (d) => <>{decisionLabel(d.decision)} <StatusBadge>{d.to_status}</StatusBadge></> },
              { label: t("Category"), hideSm: true, render: (d) => (CATEGORIES[d.category] ? t(CATEGORIES[d.category]) : d.category) },
              { label: t("Decided by"), hideSm: true, render: (d) => (d.actor_kind === "system" ? actorText({ system: true, actor: d.actor }) : d.actor || t("Admin")) },
              { label: t("Policy"), hideSm: true, render: (d) => (d.policy_source ? `${d.policy_source}${d.policy_revision != null ? ` r${d.policy_revision}` : ""}` : "-") },
              { label: t("Reason"), hideSm: true, render: (d) => d.reason || "-" },
            ]} />
          )}
          <Pagination busy={pending} page={{ offset, limit, count: page.items.length, hasMore: page.hasMore }} onChange={(o) => goTo(ROUTE, { ...f, limit: query.limit, offset: o || "" })} />
        </div>
      )}
      <Badge>{t("Read-only")}</Badge>
    </Card>
  );
}
