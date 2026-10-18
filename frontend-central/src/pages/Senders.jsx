import { useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { AuditTimeline, Button, Card, DetailList, DetailRow, Empty, ErrorState, FilterBar, Pagination, Skeleton, StatusBadge, Table, Time, useLoad } from "../ui.jsx";
import { ACTION_ARIA, ACTION_LABEL, AutoNote, DecisionDialog, EffectiveSummary, EnterpriseRef, actorText, decisionLabel, fetchEffective, shortId } from "../governance/components.jsx";
import { SENDER_KINDS, SENDER_STATUSES, actionsFor, adaptDecision, adaptPage, adaptSender } from "../governance/model.js";
import { UUID, buildHash } from "../routes.js";

const SIZES = [25, 50, 100, 200];
const go = (query) => { location.hash = buildHash("senders", [], query); };
const kindText = (k) => (k ? t(k) : "-");

function SenderRegistry({ query }) {
  const limit = SIZES.includes(Number(query.limit)) ? Number(query.limit) : 50;
  const offset = Math.max(0, Number.parseInt(query.offset, 10) || 0);
  const filters = { status: query.status || "", country: query.country || "", sender_kind: query.sender_kind || "", enterprise_id: query.enterprise_id || "" };
  const key = JSON.stringify({ limit, offset, ...filters });
  const list = useLoad(async (signal) => ({ key, page: adaptPage(await api.page("/admin/senders", { limit, offset, ...filters }, { signal }), adaptSender) }), [key]);
  const [draft, setDraft] = useState(filters);
  const pending = list.data && list.data.key !== key; // filtri/faqja ka ndryshuar, përgjigjja e re nuk ka ardhur
  const page = list.data?.page;
  const apply = (e) => { e.preventDefault(); go({ ...draft, country: draft.country.trim().toUpperCase(), enterprise_id: draft.enterprise_id.trim(), limit: limit === 50 ? "" : limit }); };
  const clear = () => { setDraft({ status: "", country: "", sender_kind: "", enterprise_id: "" }); go({}); };
  const filtering = Object.values(filters).some(Boolean);
  return (
    <Card title={t("Sender Registry")}>
      <form onSubmit={apply}>
        <FilterBar label={t("Filter senders")}>
          <select aria-label={t("Status")} value={draft.status} onChange={(e) => setDraft({ ...draft, status: e.target.value })}>
            <option value="">{t("All statuses")}</option>{SENDER_STATUSES.map((s) => <option key={s} value={s}>{t(s)}</option>)}
          </select>
          <input aria-label={t("Country")} placeholder={t("Country (ISO code)")} maxLength={2} value={draft.country} onChange={(e) => setDraft({ ...draft, country: e.target.value })} />
          <select aria-label={t("Type")} value={draft.sender_kind} onChange={(e) => setDraft({ ...draft, sender_kind: e.target.value })}>
            <option value="">{t("All types")}</option>{SENDER_KINDS.map((k) => <option key={k} value={k}>{t(k)}</option>)}
          </select>
          <input aria-label={t("Enterprise ID")} placeholder={t("Enterprise ID")} value={draft.enterprise_id} onChange={(e) => setDraft({ ...draft, enterprise_id: e.target.value })} />
          <Button variant="primary">{t("Apply filters")}</Button>
          {filtering && <Button type="button" onClick={clear}>{t("Clear filters")}</Button>}
          <select aria-label={t("Rows per page")} value={limit} onChange={(e) => go({ ...filters, limit: e.target.value === "50" ? "" : e.target.value })}>
            {SIZES.map((n) => <option key={n} value={n}>{t("{n} per page", { n })}</option>)}
          </select>
        </FilterBar>
      </form>
      {list.error && !page ? <ErrorState error={list.error} retry={list.reload} /> : list.loading && !page ? <Skeleton rows={5} /> : (
        <div aria-busy={pending || undefined} style={{ opacity: pending ? 0.55 : 1 }}>
          {list.stale && <ErrorState stale error={list.error} retry={list.reload} />}
          {page.items.length === 0 ? (
            offset > 0 ? <Empty title={t("No more results")} action={<Button onClick={() => go({ ...filters, limit: query.limit })}>{t("Back to first page")}</Button>}>{t("This page is past the end of the list.")}</Empty>
              : filtering ? <Empty title={t("No senders match your filters")} action={<Button onClick={clear}>{t("Clear filters")}</Button>}>{t("Try different filters.")}</Empty>
                : <Empty title={t("No senders yet")}>{t("Sender requests from accounts appear here.")}</Empty>
          ) : (
            <Table rows={page.items} cols={[
              { label: t("Sender"), render: (r) => <a href={buildHash("senders", [r.id])} aria-label={t("Details for {sender}", { sender: `${r.displayValue} (${r.country})` })}><b>{r.displayValue}</b></a> },
              { label: t("Enterprise"), render: (r) => <EnterpriseRef id={r.enterpriseId} /> },
              { label: t("Country"), key: "country" },
              { label: t("Type"), hideSm: true, render: (r) => kindText(r.kind) },
              { label: t("Status"), render: (r) => <StatusBadge>{r.status}</StatusBadge> },
              { label: t("Source"), hideSm: true, render: (r) => r.source || "-" },
              { label: t("Created"), hideSm: true, render: (r) => <Time value={r.createdAt} /> },
              { label: t("Updated"), hideSm: true, render: (r) => <Time value={r.updatedAt} /> },
              { label: "", render: (r) => <a className="btn small" href={buildHash("senders", [r.id])} aria-label={t("Details for {sender}", { sender: `${r.displayValue} (${r.country})` })}>{t("Details")}</a> },
            ]} />
          )}
          <Pagination busy={pending} page={{ offset, limit, count: page.items.length, hasMore: page.hasMore }} onChange={(o) => go({ ...filters, limit: query.limit, offset: o || "" })} />
        </div>
      )}
    </Card>
  );
}

function SenderDetail({ id, write }) {
  const sender = useLoad(async (signal) => adaptSender(await api.get(`/admin/senders/${id}`, {}, { signal })), [id]);
  const history = useLoad(async (signal) => (await api.get(`/admin/senders/${id}/history`, {}, { signal })).items.map(adaptDecision), [id]);
  const s = sender.data;
  const eff = useLoad(async (signal) => (s ? fetchEffective(s.country, s.kind, signal) : null), [s?.country, s?.kind]);
  const [dialog, setDialog] = useState(null);
  const refresh = async () => { await Promise.all([sender.reload(), history.reload(), eff.reload()]); };
  if (!UUID.test(id)) return null;
  const last = history.data && history.data[history.data.length - 1];
  const events = (history.data || []).map((d) => ({
    id: d.id, at: d.at, title: decisionLabel(d.decision), status: d.toStatus, actor: actorText(d),
    detail: [d.reason, d.evidenceRef && t("Evidence: {ref}", { ref: d.evidenceRef }), d.policySource && t("Policy: {source}{rev}", { source: d.policySource, rev: d.policyRevision != null ? ` r${d.policyRevision}` : "" }), d.category && t("Category: {category}", { category: d.category })].filter(Boolean).join(" · ") || null,
  }));
  return (
    <>
      <p><a href={buildHash("senders")}>← {t("Back to Sender Registry")}</a></p>
      {sender.loading && !s ? <Skeleton rows={5} /> : sender.error && !s ? <ErrorState error={sender.error} retry={sender.error.retryable ? sender.reload : undefined} /> : (
        <>
          {sender.stale && <ErrorState stale error={sender.error} retry={sender.reload} />}
          <AutoNote decision={last} />
          <Card title={s.displayValue} subtitle={`${s.country} · ${kindText(s.kind)}`}
            actions={write ? actionsFor(s.status).map((a) => (
              <Button key={a} variant={a === "approve" || a === "resubmit" ? "primary" : "danger"} className="small" aria-label={t(ACTION_ARIA[a], { sender: `${s.displayValue} (${s.country})` })} onClick={() => setDialog(a)}>{t(ACTION_LABEL[a])}</Button>
            )) : null}>
            {!write && <small className="readonly-note">{t("Read-only access: your role can view but not change senders.")}</small>}
            <DetailList>
              <DetailRow label={t("Status")}><StatusBadge>{s.status}</StatusBadge></DetailRow>
              <DetailRow label={t("Enterprise")}><span className="mono">{s.enterpriseId}</span> <a href={buildHash("senders", [], { enterprise_id: s.enterpriseId })}>{t("All senders of this enterprise")}</a></DetailRow>
              <DetailRow label={t("External reference")}><span className="mono">{s.externalRef}</span></DetailRow>
              <DetailRow label={t("Source")}>{s.source}</DetailRow>
              <DetailRow label={t("Current decision")}>{last ? <>{decisionLabel(last.decision)} · {actorText(last)} · <Time value={last.at} /></> : null}</DetailRow>
              <DetailRow label={t("Decision policy")}>{last && last.policySource ? `${last.policySource}${last.policyRevision != null ? ` r${last.policyRevision}` : ""}` : null}</DetailRow>
              <DetailRow label={t("Evidence reference")}>{last?.evidenceRef}</DetailRow>
              <DetailRow label={t("Created")}>{s.createdAt ? <Time value={s.createdAt} /> : null}</DetailRow>
              <DetailRow label={t("Updated")}>{s.updatedAt ? <Time value={s.updatedAt} /> : null}</DetailRow>
            </DetailList>
          </Card>
          <Card title={t("Current policy for this country and kind")} subtitle={t("Calculated by the server.")}>
            {eff.error && !eff.data ? <ErrorState error={eff.error} retry={eff.reload} /> : eff.data ? <EffectiveSummary eff={eff.data} /> : <Skeleton rows={2} />}
          </Card>
          {write && (
            <Card title={t("Diagnostics")} subtitle={t("Visible to admins only.")}>
              <DetailList>
                <DetailRow label={t("Normalized value")}><span className="mono">{s.normValue}</span></DetailRow>
                <DetailRow label={t("Approved key")}><span className="mono">{s.approvedKey}</span></DetailRow>
                <DetailRow label={t("Decision ID")}><span className="mono">{shortId(s.currentDecisionId)}</span></DetailRow>
              </DetailList>
            </Card>
          )}
          <Card title={t("Decision history")}>
            {history.error && !history.data ? <ErrorState error={history.error} retry={history.reload} /> : history.loading && !history.data ? <Skeleton rows={3} /> : <AuditTimeline events={events} />}
          </Card>
          {dialog && <DecisionDialog action={dialog} sender={s} onClose={() => setDialog(null)} onDone={refresh} onConflict={refresh} />}
        </>
      )}
    </>
  );
}

export default function Senders({ write, rest = [], query = {} }) {
  if (rest[0]) return <SenderDetail id={rest[0]} write={write} />;
  return <SenderRegistry query={query} />;
}
