import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { Button, Card, Empty, ErrorState, Pagination, Skeleton, Table, Time, useLoad } from "../ui.jsx";
import { DateFilter, ExportButton, Filters, goTo } from "./components.jsx";

const SIZES = [25, 50, 100, 200];
const SCOPES = { all: T("All areas"), sender: T("Sender governance"), commercial: T("Commercial"), identity: T("Accounts and access") };
const detailText = (d) => (d ? JSON.stringify(d) : "");

/** Lista e përbashkët e audit-it të stafit (burimi: `audit_log`). `fixedScope` e fikson fushën (Commercial); pa të, përdoruesi e zgjedh. */
export default function AuditLog({ routeId, title, subtitle, fixedScope, query }) {
  const limit = SIZES.includes(Number(query.limit)) ? Number(query.limit) : 50;
  const offset = Math.max(0, Number.parseInt(query.offset, 10) || 0);
  const scope = fixedScope || (SCOPES[query.scope] ? query.scope : "all");
  const f = { scope, action: query.action || "", resource_type: query.resource_type || "", actor_kind: query.actor_kind === "system" || query.actor_kind === "user" ? query.actor_kind : "", from: query.from || "", to: query.to || "" };
  const key = JSON.stringify({ limit, offset, ...f });
  const facets = useLoad((signal) => api.get("/admin/audit/facets", { scope }, { signal }), [scope]);
  const list = useLoad(async (signal) => ({ key, page: await api.page("/admin/audit", { limit, offset, ...f }, { signal }) }), [key]);
  const page = list.data?.page, pending = list.data && list.data.key !== key;
  const set = (patch) => goTo(routeId, { ...(fixedScope ? {} : { scope: f.scope === "all" ? "" : f.scope }), action: f.action, resource_type: f.resource_type, actor_kind: f.actor_kind, from: f.from, to: f.to, limit: query.limit, ...patch, offset: "" });
  const filtering = !!(f.action || f.resource_type || f.actor_kind || f.from || f.to || (!fixedScope && scope !== "all"));
  const actions = facets.data?.actions || [];
  return (
    <Card title={title} subtitle={subtitle} actions={<ExportButton path="/admin/audit.csv" params={f} file="central-audit.csv" />}>
      <Filters label={t("Filter audit")}>
        {!fixedScope && <select aria-label={t("Area")} value={scope} onChange={(e) => set({ scope: e.target.value === "all" ? "" : e.target.value })}>{Object.entries(SCOPES).map(([k, v]) => <option key={k} value={k}>{t(v)}</option>)}</select>}
        <select aria-label={t("Action")} value={f.action} onChange={(e) => set({ action: e.target.value })}><option value="">{t("All actions")}</option>{actions.map((a) => <option key={a} value={a}>{a}</option>)}</select>
        <select aria-label={t("Item type")} value={f.resource_type} onChange={(e) => set({ resource_type: e.target.value })}><option value="">{t("All item types")}</option>{(facets.data?.resource_types || []).map((a) => <option key={a} value={a}>{a}</option>)}</select>
        <select aria-label={t("Actor")} value={f.actor_kind} onChange={(e) => set({ actor_kind: e.target.value })}><option value="">{t("People and system")}</option><option value="user">{t("People")}</option><option value="system">{t("System processes")}</option></select>
        <DateFilter from={f.from} to={f.to} onChange={(from, to) => set({ from, to })} />
        {filtering && <Button type="button" onClick={() => goTo(routeId, {})}>{t("Clear filters")}</Button>}
        <select aria-label={t("Rows per page")} value={limit} onChange={(e) => set({ limit: e.target.value === "50" ? "" : e.target.value })}>{SIZES.map((n) => <option key={n} value={n}>{t("{n} per page", { n })}</option>)}</select>
      </Filters>
      {list.error && !page ? <ErrorState error={list.error} retry={list.error.retryable ? list.reload : undefined} /> : !page ? <Skeleton rows={5} /> : (
        <div aria-busy={pending || undefined} style={{ opacity: pending ? 0.55 : 1 }}>
          {list.stale && <ErrorState stale error={list.error} retry={list.reload} />}
          {page.items.length === 0 ? (
            offset > 0 ? <Empty title={t("No more results")} action={<Button onClick={() => set({})}>{t("Back to first page")}</Button>}>{t("This page is past the end of the list.")}</Empty>
              : <Empty title={filtering ? t("No entries match your filters") : t("Nothing recorded yet")}>{filtering ? t("Try different filters.") : t("Staff and system actions will be listed here.")}</Empty>
          ) : (
            <Table rows={page.items} cols={[
              { label: t("When"), render: (r) => <Time value={r.at} /> },
              { label: t("Who"), render: (r) => (r.actor_kind === "system" ? <><b>{r.actor}</b> <small className="muted">{t("system")}</small></> : r.actor || "-") },
              { label: t("Action"), render: (r) => <code>{r.action}</code> },
              { label: t("Item"), hideSm: true, render: (r) => <span className="mono">{r.resource_type} {r.resource_id}</span> },
              { label: t("Details"), hideSm: true, render: (r) => <small className="muted">{detailText(r.detail).slice(0, 160)}</small> },
            ]} />
          )}
          <Pagination busy={pending} page={{ offset, limit, count: page.items.length, hasMore: page.hasMore }} onChange={(o) => goTo(routeId, { ...f, scope: fixedScope ? "" : f.scope === "all" ? "" : f.scope, limit: query.limit, offset: o || "" })} />
        </div>
      )}
    </Card>
  );
}
