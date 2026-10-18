import { useEffect, useState } from "react";
import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { failureInfo } from "../messaging/format.js";
import { useHashState } from "../messaging/hashstate.js";
import { DateRange, SplitBar } from "../reports/components.jsx";
import { rangeFrom } from "../reports/range.js";
import { Badge, Button, Card, ErrorBox, ErrorState, Skeleton, Stat, Table, Tabs, Time, dateOnly, money, useAction, useDebounced, useLoad } from "../ui.jsx";

const TABS = [{ id: "overview", label: T("Overview") }, { id: "campaigns", label: T("Campaigns") }, { id: "failures", label: T("Delivery & failures") }, { id: "usage", label: T("Usage") }];
const PAGE = 25;
const STATUSES = ["draft", "scheduled", "preparing", "running", "paused", "completed", "cancelled"];
const pct = (v) => (v == null ? "-" : `${v}%`);
const sum = (o, ...keys) => keys.reduce((n, k) => n + (o?.[k] || 0), 0);
const stamp = (r) => `${r.from}-${r.to}`;

// Të dhëna vetëm kur i përkasin filtrave AKTUALË: një përgjigje e vjetër (data/campaign tjetër) nuk shfaqet kurrë nën filtra të rinj, as kur kërkesa e re dështon.
function useReport(path, params) {
  const key = JSON.stringify([path, params]);
  const r = useLoad(async (signal) => ({ key, body: await api.get(path, params, { signal }) }), [key]);
  return { ...r, data: r.data && r.data.key === key ? r.data.body : null };
}

// Jo hook: kthen vetëm trajtuesin e klikimit (shkarkimi bëhet nga serveri, i kufizuar dhe me filtra).
const exportHandler = (a, path, params, file) => () => a.run(() => api.download(path, params, file), t("Download started"));

function Overview({ range }) {
  const d = useReport("/v1/reports/overview", range);
  const r = d.data;
  const by = r?.messages.by_status || {};
  if (d.error && !r) return <ErrorState error={d.error} retry={d.reload} />;
  if (!r) return <Skeleton rows={4} />;
  const camp = r.campaigns.by_status;
  return (
    <>
      <Card title={t("Messages")} subtitle={t("Days are UTC; both end dates are included.")} actions={<Button onClick={d.reload}>{t("Refresh")}</Button>}>
        <ErrorBox error={d.error} retry={d.reload} />
        <div className="grid stats">
          <Stat label={t("SMS submitted")} value={r.messages.total} />
          <Stat label={t("Delivered")} value={by.delivered || 0} />
          <Stat label={t("Failed")} value={by.failed || 0} />
          <Stat label={t("Still sending")} value={sum(by, "queued", "sending", "sent")} sub={t("Waiting for the carrier or a final status.")} />
          <Stat label={t("Delivery rate")} value={pct(r.messages.delivery_rate)} sub={t("Delivered ÷ (delivered + failed). Messages still in progress are not counted.")} />
        </div>
        {!r.messages.total && <p className="note">{t("No SMS in this period.")}</p>}
      </Card>
      <Card title={t("Campaigns created in this period")}>
        <div className="grid stats">
          {STATUSES.filter((s) => camp[s]).map((s) => <Stat key={s} label={t(s)} value={camp[s]} />)}
          {!Object.keys(camp).length && <p className="note">{t("No campaigns in this period.")}</p>}
        </div>
        <Table rows={r.recent_campaigns.map((c) => ({ ...c, _onClick: () => { location.hash = `#campaigns/${c.id}`; } }))} emptyTitle={t("No campaigns")} empty={t("Campaigns you create will show up here.")}
          cols={[{ label: t("Campaign"), render: (c) => <b>{c.name}</b> }, { label: t("Status"), render: (c) => <Badge>{c.status}</Badge> }, { label: t("Recipients"), num: true, key: "recipients_total" }, { label: t("Delivery rate"), num: true, hideSm: true, render: (c) => pct(c.delivery_rate) }]} />
      </Card>
      <Card title={t("Top failure reasons")}>
        <FailureTable rows={r.top_failures} total={by.failed || 0} />
        {r.failures_truncated && <p className="note">{t("Only the largest groups are shown.")}</p>}
      </Card>
    </>
  );
}

function FailureTable({ rows, total }) {
  return (
    <Table rows={rows.map((x) => ({ ...x, id: `${x.status}:${x.error_code}` }))} emptyTitle={t("No failures")} empty={t("Nothing failed in this period.")}
      cols={[{ label: t("Reason"), render: (x) => { const f = failureInfo(x.error_code); return f ? <span title={f.code}>{f.label}</span> : t("No error code"); } },
        { label: t("Code"), hideSm: true, render: (x) => (x.error_code ? <code>{x.error_code}</code> : "-") },
        { label: t("Status"), hideSm: true, render: (x) => <Badge>{x.status}</Badge> },
        { label: t("Messages"), num: true, key: "count" },
        { label: t("Share of failed"), num: true, hideSm: true, render: (x) => (x.status === "failed" && total ? `${Math.round((x.count * 1000) / total) / 10}%` : "-") }]} />
  );
}

function Campaigns({ range, query, hs }) {
  const status = STATUSES.includes(query.status) ? query.status : "", q = query.q || "", before = query.before || "";
  const [draft, setDraft] = useState(q);
  const dq = useDebounced(draft);
  const a = useAction();
  const go = (patch) => hs.go([], { ...query, before: "", ...patch });
  useEffect(() => { if (dq.trim() !== q) go({ q: dq.trim() }); }, [dq]); // eslint-disable-line
  const key = JSON.stringify({ ...range, status, q, before });
  const list = useLoad(async (signal) => ({ key, rows: await api.get("/v1/reports/campaigns", { ...range, status, q, before_id: before || undefined, limit: PAGE + 1 }, { signal }) }), [key]);
  const all = list.data?.rows || null, rows = all ? all.slice(0, PAGE) : null, more = all && all.length > PAGE;
  const filtering = !!(status || q);
  const exportCsv = exportHandler(a, "/v1/reports/campaigns.csv", { ...range, status, q }, `campaigns-${stamp(range)}.csv`);
  return (
    <Card title={t("Campaign performance")} subtitle={t("Counts come from the server. Delivery rate = delivered ÷ (delivered + failed).")} actions={<Button busy={a.busy} onClick={exportCsv}>{t("Export CSV")}</Button>}>
      <div className="toolbar" role="search" aria-label={t("Filter campaigns")}>
        <input type="search" aria-label={t("Search")} placeholder={t("Search by campaign name")} value={draft} onChange={(e) => setDraft(e.target.value)} />
        <select aria-label={t("Filter by status")} value={status} onChange={(e) => go({ status: e.target.value })}>
          <option value="">{t("All statuses")}</option>{STATUSES.map((s) => <option key={s} value={s}>{t(s)}</option>)}
        </select>
        {filtering && <Button onClick={() => { setDraft(""); hs.go([], { tab: "campaigns", from: range.from, to: range.to }); }}>{t("Clear filters")}</Button>}
        <Button onClick={list.reload}>{t("Refresh")}</Button>
      </div>
      <ErrorBox error={a.error} />
      {list.error && !rows ? <ErrorState error={list.error} retry={list.reload} /> : !rows ? <Skeleton rows={5} /> : (
        <>
          {list.stale && <ErrorBox error={list.error} retry={list.reload} />}
          <Table rows={rows.map((c) => ({ ...c, _onClick: () => { location.hash = `#campaigns/${c.id}`; } }))} emptyTitle={filtering ? t("No matches") : t("No campaigns in this period")} empty={filtering ? t("Try a different search or clear the filter.") : t("Choose a wider date range.")}
            cols={[{ label: t("Campaign"), render: (c) => <b>{c.name}</b> }, { label: t("Status"), render: (c) => <Badge>{c.status}</Badge> },
              { label: t("Created"), hideSm: true, render: (c) => <Time value={c.created_at} /> }, { label: t("Recipients"), num: true, key: "recipients_total" },
              { label: t("Delivered"), num: true, hideSm: true, render: (c) => c.message_counts.delivered || 0 },
              { label: t("Failed"), num: true, hideSm: true, render: (c) => sum(c.message_counts, "failed", "bounced") },
              { label: t("In progress"), num: true, hideSm: true, render: (c) => sum(c.message_counts, "queued", "sending", "sent") },
              { label: t("Delivery rate"), num: true, render: (c) => pct(c.delivery_rate) }]} />
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

function Failures({ range, query, hs }) {
  const campaign = /^\d+$/.test(query.campaign || "") ? query.campaign : "";
  const a = useAction();
  const d = useReport("/v1/reports/failures", { ...range, campaign_id: campaign || undefined });
  const r = d.data;
  const exportCsv = exportHandler(a, "/v1/reports/messages.csv", { ...range, status: "failed", campaign_id: campaign || undefined }, `failed-messages-${stamp(range)}.csv`);
  const failed = r?.by_status.failed || 0;
  const cats = {};
  for (const x of r?.rows || []) { const f = failureInfo(x.error_code); const k = f ? f.label : t("No error code"); cats[k] = (cats[k] || 0) + x.count; }
  return (
    <>
      <Card title={t("Delivery & failures")} subtitle={t("Status and error code of every SMS in the period, counted by the server.")}
        actions={<><Button busy={a.busy} onClick={exportCsv}>{t("Export failed messages (CSV)")}</Button><Button onClick={d.reload}>{t("Refresh")}</Button></>}>
        <div className="toolbar" role="search" aria-label={t("Filter failures")}>
          <input aria-label={t("Campaign number")} placeholder={t("Campaign number")} inputMode="numeric" defaultValue={campaign}
            onBlur={(e) => /^\d*$/.test(e.target.value) && e.target.value !== campaign && hs.go([], { ...query, campaign: e.target.value })}
            onKeyDown={(e) => e.key === "Enter" && /^\d*$/.test(e.target.value) && hs.go([], { ...query, campaign: e.target.value })} />
          {campaign && <Button onClick={() => hs.go([], { ...query, campaign: "" })}>{t("Clear filters")}</Button>}
        </div>
        <ErrorBox error={a.error} />
        {d.error && !r ? <ErrorState error={d.error} retry={d.reload} /> : !r ? <Skeleton rows={4} /> : (
          <>
            <div className="grid stats">
              <Stat label={t("Messages")} value={r.total_messages} />
              {["delivered", "failed", "sending", "sent", "queued"].filter((s) => r.by_status[s]).map((s) => <Stat key={s} label={t(s)} value={r.by_status[s]} />)}
            </div>
            {r.truncated && <p className="note">{t("Only the largest groups are shown.")}</p>}
          </>
        )}
      </Card>
      {r && (
        <>
          <Card title={t("By category")}>
            <Table rows={Object.entries(cats).map(([label, count]) => ({ id: label, label, count }))} emptyTitle={t("No failures")} empty={t("Nothing failed in this period.")}
              cols={[{ label: t("Category"), key: "label" }, { label: t("Messages"), num: true, key: "count" }, { label: t("Share of failed"), num: true, render: (x) => (failed ? `${Math.round((x.count * 1000) / failed) / 10}%` : "-") }]} />
          </Card>
          <Card title={t("By error code")}><FailureTable rows={r.rows} total={failed} /></Card>
        </>
      )}
    </>
  );
}

function Usage({ range }) {
  const a = useAction();
  const d = useReport("/v1/reports/usage", range);
  const u = d.data;
  const days = u ? [...u.days].reverse().filter((x) => x.sms.count || x.email.count) : [];
  const max = u ? Math.max(1, ...u.days.map((x) => x.sms.count)) : 1;
  const dl = (kind) => exportHandler(a, `/v1/reports/${kind}.csv`, range, `${kind}-${stamp(range)}.csv`);
  return (
    <>
      <Card title={t("Usage")} subtitle={t("What you sent and what it cost. Days are in UTC.")}
        actions={<><Button busy={a.busy} onClick={dl("usage")}>{t("Export usage (CSV)")}</Button><Button busy={a.busy} onClick={dl("messages")}>{t("Export SMS (CSV)")}</Button><Button busy={a.busy} onClick={dl("emails")}>{t("Export emails (CSV)")}</Button></>}>
        <ErrorBox error={a.error} />
        {d.error && !u ? <ErrorState error={d.error} retry={d.reload} /> : !u ? <Skeleton rows={3} /> : (
          <div className="grid stats">
            <Stat label={t("SMS sent")} value={u.totals.sms.count} sub={t("{n} delivered · {m} failed", { n: u.totals.sms.delivered, m: u.totals.sms.failed })} />
            <Stat label={t("SMS cost (delivered)")} value={money(u.totals.sms.cost)} sub={t("{n} parts", { n: u.totals.sms.segments })} />
            <Stat label={t("Emails sent")} value={u.totals.email.count} sub={t("{n} delivered · {m} bounced", { n: u.totals.email.delivered, m: u.totals.email.bounced })} />
          </div>
        )}
      </Card>
      <Card title={t("By day")}>
        <div className="legend" aria-hidden="true"><span><i className="ok" />{t("Delivered")}</span><span><i className="bad" />{t("Failed")}</span><span><i />{t("Other")}</span></div>
        <Table rows={days.map((x) => ({ ...x, id: x.date }))} loading={d.loading && !u} emptyTitle={t("No activity in this period")} empty={t("Messages you send will show up here.")}
          cols={[{ label: t("Day"), render: (x) => dateOnly(x.date) },
            { label: t("Volume"), render: (x) => <SplitBar delivered={x.sms.delivered} failed={x.sms.failed} other={Math.max(0, x.sms.count - x.sms.delivered - x.sms.failed)} max={max} /> },
            { label: "SMS", num: true, render: (x) => x.sms.count }, { label: t("Delivered"), num: true, render: (x) => x.sms.delivered },
            { label: t("Failed"), num: true, render: (x) => x.sms.failed }, { label: t("Cost"), num: true, hideSm: true, render: (x) => money(x.sms.cost) },
            { label: "Email", num: true, hideSm: true, render: (x) => x.email.count }]} />
      </Card>
    </>
  );
}

export default function Reports() {
  const hs = useHashState("reports");
  const { query } = hs;
  const tab = TABS.some((x) => x.id === query.tab) ? query.tab : "overview";
  const range = rangeFrom(query);
  const setTab = (id) => hs.go([], { tab: id, from: query.from, to: query.to });
  return (
    <>
      <Tabs tabs={TABS.map((x) => ({ ...x, label: t(x.label) }))} value={tab} onChange={setTab} />
      <Card><DateRange from={range.from} to={range.to} onChange={(from, to) => hs.go([], { ...query, from, to, before: "" })} /></Card>
      {tab === "overview" && <Overview range={range} />}
      {tab === "campaigns" && <Campaigns range={range} query={query} hs={hs} />}
      {tab === "failures" && <Failures range={range} query={query} hs={hs} />}
      {tab === "usage" && <Usage range={range} />}
    </>
  );
}
