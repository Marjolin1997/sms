import { useEffect, useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { Badge, Button, Card, ErrorBox, ErrorState, Skeleton, Table, Tabs, Time, money, useDebounced, useLoad } from "../ui.jsx";
import { failureInfo } from "../messaging/format.js";
import { useHashState } from "../messaging/hashstate.js";
import { MessageTimeline } from "./Campaigns.jsx";

const SMS_STATUS = ["", "queued", "sending", "sent", "delivered", "failed"];
const EMAIL_STATUS = ["", "queued", "sent", "delivered", "bounced", "complained", "failed"];
const PAGE = 30;

function EmailTimeline({ path }) {
  const [rows, setRows] = useState(null);
  useEffect(() => { api.get(path).then(setRows).catch(() => setRows([])); }, [path]);
  return <Table rows={rows || []} loading={!rows} empty={t("No history.")} cols={[{ label: t("Status"), render: (r) => <Badge>{r.to}</Badge> }, { label: t("Detail"), render: (r) => r.detail || "" }]} />;
}

export default function Messages() {
  const hs = useHashState("messages");
  const { query } = hs;
  const kind = query.kind === "email" ? "email" : "sms";
  const status = query.status || "", q = query.q || "", before = query.before || "", open = query.msg || "";
  const sender = query.sender || "", campaign = query.campaign || "", from = query.from || "", to = query.to || "";
  const [draft, setDraft] = useState(q);
  const dq = useDebounced(draft);
  const [dateError, setDateError] = useState("");
  const go = (patch) => hs.go([], { ...query, before: "", msg: "", ...patch });
  useEffect(() => { if (dq.trim() !== q) go({ q: dq.trim() }); }, [dq]); // eslint-disable-line
  const sms = kind === "sms";
  const path = sms ? "/v1/messages" : "/v1/email/messages";
  const key = JSON.stringify({ kind, status, q, before, sender, campaign, from, to });
  const params = sms ? { status, q, before_id: before || undefined, limit: PAGE, sender, campaign_id: campaign, created_from: from, created_to: to } : { status, q, before_id: before || undefined, limit: PAGE };
  const list = useLoad(async (signal) => ({ key, ...(await api.get(path, params, { signal })) }), [key]);
  const items = list.data ? list.data.items : null;
  const filtering = !!(status || q || sender || campaign || from || to);
  const setDate = (k, v) => {
    const f = k === "from" ? v : from, e = k === "to" ? v : to;
    if (f && e && f > e) { setDateError(t("The end date must not be before the start date.")); return; }
    setDateError(""); go({ [k]: v });
  };
  const cols = sms
    ? [{ label: t("To"), render: (r) => <b>+{r.to}</b> }, { label: t("From"), hideSm: true, key: "sender" }, { label: t("Status"), render: (r) => <Badge>{r.status}</Badge> },
       { label: t("Why not delivered"), hideSm: true, render: (r) => { const f = failureInfo(r.error_code); return f ? <span title={f.code}>{f.label}</span> : "-"; } },
       { label: t("Parts"), key: "segments", num: true, hideSm: true }, { label: t("Cost"), num: true, hideSm: true, render: (r) => `${money(r.total_price)} ${r.currency}` }, { label: t("Sent"), render: (r) => <Time value={r.created_at} /> }]
    : [{ label: t("To"), render: (r) => <b>{r.to}</b> }, { label: t("Subject"), hideSm: true, key: "subject" }, { label: t("Status"), render: (r) => <Badge>{r.status}</Badge> }, { label: t("Type"), hideSm: true, render: (r) => t(r.category) }, { label: t("Note"), hideSm: true, render: (r) => r.error_code || "" }, { label: t("Sent"), render: (r) => <Time value={r.created_at} /> }];
  return (
    <>
      <Tabs tabs={[{ id: "sms", label: "SMS" }, { id: "email", label: "Email" }]} value={kind} onChange={(k) => hs.go([], k === "sms" ? {} : { kind: k })} />
      <Card>
        <div className="toolbar" role="search" aria-label={t("Filter messages")}>
          <input aria-label={t("Search")} placeholder={sms ? t("Search by phone number") : t("Search by address or subject")} value={draft} onChange={(e) => setDraft(e.target.value)} />
          <select aria-label={t("Filter by status")} value={status} onChange={(e) => go({ status: e.target.value })}>
            {(sms ? SMS_STATUS : EMAIL_STATUS).map((s) => <option key={s} value={s}>{s ? t("Status: {status}", { status: t(s) }) : t("All statuses")}</option>)}
          </select>
          {sms && <>
            <input aria-label={t("Sender ID")} placeholder={t("Sender ID")} maxLength={16} defaultValue={sender} onBlur={(e) => e.target.value.trim() !== sender && go({ sender: e.target.value.trim() })} onKeyDown={(e) => e.key === "Enter" && go({ sender: e.target.value.trim() })} />
            <input aria-label={t("Campaign number")} placeholder={t("Campaign number")} inputMode="numeric" defaultValue={campaign} onBlur={(e) => /^\d*$/.test(e.target.value) && e.target.value !== campaign && go({ campaign: e.target.value })} onKeyDown={(e) => e.key === "Enter" && /^\d*$/.test(e.target.value) && go({ campaign: e.target.value })} />
            <input type="date" aria-label={t("From date")} value={from} onChange={(e) => setDate("from", e.target.value)} />
            <input type="date" aria-label={t("To date")} value={to} onChange={(e) => setDate("to", e.target.value)} />
          </>}
          {filtering && <Button onClick={() => { setDraft(""); setDateError(""); hs.go([], kind === "email" ? { kind } : {}); }}>{t("Clear filters")}</Button>}
          <Button onClick={() => list.reload()}>{t("Refresh")}</Button>
        </div>
        {dateError && <div className="alert warn" role="alert"><span>{dateError}</span></div>}
        {list.error && !items ? <ErrorState error={list.error} retry={list.reload} /> : list.loading && !items ? <Skeleton rows={5} /> : (
          <>
            {list.stale && <ErrorBox error={list.error} retry={list.reload} />}
            <Table rows={(items || []).map((r) => ({ ...r, _onClick: () => go({ msg: open === r.id ? "" : r.id, before }) }))} emptyTitle={filtering ? t("No matches") : t("No messages yet")} empty={filtering ? t("Try a different search or clear the filter.") : t("Messages you send appear here with their delivery status.")} cols={cols} />
            {(before || list.data?.next_before_id) && (
              <nav className="pager" aria-label={t("Pagination")}>
                <Button disabled={!before} onClick={() => go({ before: "" })}>{t("Newest")}</Button>
                <Button disabled={!list.data?.next_before_id} onClick={() => go({ before: String(list.data.next_before_id) })}>{t("Older")}</Button>
              </nav>
            )}
          </>
        )}
      </Card>
      {open && (
        <Card title={t("What happened")} subtitle={t("Reference {id}", { id: open })} actions={<Button onClick={() => go({ msg: "", before })}>{t("Close")}</Button>}>
          {sms ? <MessageTimeline publicId={open} /> : <EmailTimeline path={`/v1/email/messages/${open}/events`} />}
        </Card>
      )}
    </>
  );
}
