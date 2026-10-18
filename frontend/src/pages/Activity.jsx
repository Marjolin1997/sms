import { useState } from "react";
import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { useHashState } from "../messaging/hashstate.js";
import { DateRange } from "../reports/components.jsx";
import { rangeFrom } from "../reports/range.js";
import { Badge, Button, Card, ErrorBox, ErrorState, Skeleton, Table, Time, useAction, useLoad } from "../ui.jsx";

const PAGE = 30;
// Etiketa njerëzore për veprimet më të zakonshme; veprimi i panjohur shfaqet siç është (kod), kurrë i fshehur.
const LABELS = {
  "campaign.create": T("Campaign created"), "campaign.schedule": T("Campaign started or scheduled"), "campaign.pause": T("Campaign paused"),
  "campaign.resume": T("Campaign resumed"), "campaign.cancel": T("Campaign cancelled"), "sender.request": T("Sender ID requested"),
  "template.create": T("Template created"), "template.version": T("Template version added"), "webhook.create": T("Webhook created"),
  "webhook.update": T("Webhook changed"), "webhook.delete": T("Webhook deleted"), "webhook.rotate_secret": T("Webhook secret rotated"),
  "apikey.create": T("API key created"), "apikey.self_create": T("API key created"), "apikey.self_revoke": T("API key revoked"), "apikey.revoke": T("API key revoked"),
  "contact.export": T("Contact exported"), "contact.erase": T("Contact erased"), "report.export": T("Report exported"), "billing.profile": T("Billing details changed"),
};
const BY = { you: T("You"), staff: T("Our staff"), system: T("System") };
const label = (a) => (LABELS[a] ? t(LABELS[a]) : a);
const detailText = (d) => Object.entries(d || {}).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : typeof v === "object" && v ? JSON.stringify(v) : v}`).join(" · ");

export default function Activity() {
  const hs = useHashState("activity");
  const { query } = hs;
  const range = rangeFrom(query);
  const action = query.action || "", type = query.type || "", before = query.before || "";
  const a = useAction();
  const go = (patch) => hs.go([], { ...query, before: "", ...patch });
  const facets = useLoad((signal) => api.get("/v1/reports/audit/facets", {}, { signal }), []);
  const key = JSON.stringify({ ...range, action, type, before });
  const list = useLoad(async (signal) => ({ key, rows: await api.get("/v1/reports/audit", { ...range, action, target_type: type, before_id: before || undefined, limit: PAGE + 1 }, { signal }) }), [key]);
  const all = list.data?.rows || null, rows = all ? all.slice(0, PAGE) : null, more = all && all.length > PAGE;
  const filtering = !!(action || type);
  const exportCsv = () => a.run(() => api.download("/v1/reports/audit.csv", { ...range, action, target_type: type }, `activity-${range.from}-${range.to}.csv`), t("Download started"));
  return (
    <Card title={t("Activity log")} subtitle={t("Changes made to your account, newest first. Entries can't be edited or removed. Days are UTC.")} actions={<Button busy={a.busy} onClick={exportCsv}>{t("Export CSV")}</Button>}>
      <DateRange from={range.from} to={range.to} onChange={(from, to) => go({ from, to })} />
      <div className="toolbar" role="search" aria-label={t("Filter activity")}>
        <select aria-label={t("Filter by action")} value={action} onChange={(e) => go({ action: e.target.value })}>
          <option value="">{t("All actions")}</option>{(facets.data?.actions || []).map((x) => <option key={x} value={x}>{label(x)}</option>)}
        </select>
        <select aria-label={t("Filter by item type")} value={type} onChange={(e) => go({ type: e.target.value })}>
          <option value="">{t("All item types")}</option>{(facets.data?.target_types || []).map((x) => <option key={x} value={x}>{x}</option>)}
        </select>
        {filtering && <Button onClick={() => go({ action: "", type: "" })}>{t("Clear filters")}</Button>}
        <Button onClick={list.reload}>{t("Refresh")}</Button>
      </div>
      <ErrorBox error={a.error} />
      {list.error && !rows ? <ErrorState error={list.error} retry={list.reload} /> : !rows ? <Skeleton rows={5} /> : (
        <>
          {list.stale && <ErrorBox error={list.error} retry={list.reload} />}
          <Table rows={rows} emptyTitle={filtering ? t("No matches") : t("Nothing recorded in this period")} empty={filtering ? t("Try a different filter.") : t("Changes you make will be listed here. Older history may not be available.")}
            cols={[{ label: t("When"), render: (r) => <Time value={r.at} /> }, { label: t("Who"), render: (r) => <Badge>{t(BY[r.by] || BY.staff)}</Badge> },
              { label: t("What"), render: (r) => <span title={r.action}>{label(r.action)}</span> }, { label: t("Item"), hideSm: true, render: (r) => `${r.target_type} ${r.target_id}` },
              { label: t("Details"), hideSm: true, render: (r) => <small className="muted">{detailText(r.detail)}</small> }]} />
          {(before || more) && (
            <nav className="pager" aria-label={t("Pagination")}>
              <Button disabled={!before} onClick={() => go({ before: "" })}>{t("Newest")}</Button>
              <Button disabled={!more} onClick={() => go({ before: String(rows[rows.length - 1].id) })}>{t("Older")}</Button>
            </nav>
          )}
        </>
      )}
    </Card>
  );
}
