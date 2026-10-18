import { useState } from "react";
import { api } from "../api.js";
import { t } from "../i18n.jsx";
import { AuditTimeline, Button, Card, DetailList, DetailRow, Empty, ErrorBox, ErrorState, Field, FilterBar, Pagination, Skeleton, StatusBadge, Table, Time, useLoad, useUi } from "../ui.jsx";
import { EffectiveSummary, PolicyDialog, fetchEffective, shortId, yesNo } from "../governance/components.jsx";
import { SENDER_KINDS, adaptPage, adaptPolicy } from "../governance/model.js";
import { UUID, buildHash } from "../routes.js";

const SIZES = [25, 50, 100, 200];
const go = (query) => { location.hash = buildHash("policies", [], query); };
const allowedBadge = (p) => <StatusBadge>{p.allowed ? "allowed" : "denied"}</StatusBadge>;

/** Panel "Preview": Country + Kind → endpoint efektiv i serverit. Asgjë nuk llogaritet në JS dhe asgjë nuk krijohet. */
function EffectivePreview({ initial }) {
  const [f, setF] = useState({ country: initial?.country || "", kind: initial?.kind || "alphanumeric" });
  const [state, setState] = useState({ busy: false, error: null, eff: null });
  const run = async (e) => {
    e?.preventDefault();
    setState({ busy: true, error: null, eff: null });
    try { setState({ busy: false, error: null, eff: await fetchEffective(f.country.trim(), f.kind) }); }
    catch (error) { setState({ busy: false, error, eff: null }); }
  };
  return (
    <Card title={t("Effective policy preview")} subtitle={t("Asks the server what applies right now. Nothing is created.")}>
      <form onSubmit={run}>
        <FilterBar label={t("Preview effective policy")}>
          <input aria-label={t("Country (ISO code)")} placeholder={t("Country (ISO code)")} maxLength={2} value={f.country} onChange={(e) => setF({ ...f, country: e.target.value })} />
          <select aria-label={t("Sender kind")} value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}>{SENDER_KINDS.map((k) => <option key={k} value={k}>{t(k)}</option>)}</select>
          <Button variant="primary" busy={state.busy} disabled={f.country.trim().length !== 2}>{t("Preview")}</Button>
        </FilterBar>
      </form>
      {state.error && <ErrorBox error={state.error} />}
      {state.eff && <EffectiveSummary eff={state.eff} />}
    </Card>
  );
}

function PolicyList({ query, write }) {
  const { toast } = useUi();
  const limit = SIZES.includes(Number(query.limit)) ? Number(query.limit) : 50;
  const offset = Math.max(0, Number.parseInt(query.offset, 10) || 0);
  const allRevisions = query.all === "1";
  const filters = { country: query.country || "", sender_kind: query.sender_kind || "" };
  const key = JSON.stringify({ limit, offset, allRevisions, ...filters });
  const list = useLoad(async (signal) => ({ key, page: adaptPage(await api.page("/admin/sender-policies", { limit, offset, latest_only: allRevisions ? "" : "true", ...filters }, { signal }), adaptPolicy) }), [key]);
  const [draft, setDraft] = useState(filters);
  const [dialog, setDialog] = useState(null); // { initial? }
  const [result, setResult] = useState(null);
  const page = list.data?.page;
  const pending = list.data && list.data.key !== key;
  const base = { ...filters, all: allRevisions ? "1" : "", limit: query.limit };
  const apply = (e) => { e.preventDefault(); go({ ...base, country: draft.country.trim().toUpperCase(), sender_kind: draft.sender_kind }); };
  const saved = async (r) => {
    setResult(r);
    toast(r.created ? t("Policy revision {rev} saved", { rev: r.policy.revision }) : t("No change: identical to the current revision"));
    await list.reload();
  };
  return (
    <>
      {result && (
        <div className={`alert ${result.revokedSenders > 0 ? "warn" : "good"}`} role="status">
          <span>
            {result.created ? t("Policy revision {rev} saved for {country} {kind}.", { rev: result.policy.revision, country: result.policy.country, kind: t(result.policy.kind) }) : t("No change: this is identical to the current revision, so no new revision was created.")}
            {result.revokedSenders > 0 && ` ${t("{n} approved sender IDs were revoked.", { n: result.revokedSenders })}`}
          </span>
          <button className="link" onClick={() => setResult(null)}>{t("Dismiss")}</button>
        </div>
      )}
      <EffectivePreview />
      <Card title={allRevisions ? t("All policy revisions") : t("Current policies")} actions={write ? <Button variant="primary" className="small" onClick={() => setDialog({})}>{t("New policy revision")}</Button> : null}>
        {!write && <small className="readonly-note">{t("Read-only access: your role can view but not change policies.")}</small>}
        <div className="help">{t("Only explicit policies are listed. A country and kind without one uses the default: allowed, approval required.")}</div>
        <form onSubmit={apply}>
          <FilterBar label={t("Filter policies")}>
            <input aria-label={t("Country")} placeholder={t("Country (ISO code)")} maxLength={2} value={draft.country} onChange={(e) => setDraft({ ...draft, country: e.target.value })} />
            <select aria-label={t("Sender kind")} value={draft.sender_kind} onChange={(e) => setDraft({ ...draft, sender_kind: e.target.value })}><option value="">{t("All types")}</option>{SENDER_KINDS.map((k) => <option key={k} value={k}>{t(k)}</option>)}</select>
            <Button variant="primary">{t("Apply filters")}</Button>
            <label className="row"><input type="checkbox" checked={allRevisions} onChange={(e) => go({ ...base, all: e.target.checked ? "1" : "", offset: "" })} />{t("Show all revisions")}</label>
          </FilterBar>
        </form>
        {list.error && !page ? <ErrorState error={list.error} retry={list.reload} /> : list.loading && !page ? <Skeleton rows={4} /> : (
          <div aria-busy={pending || undefined} style={{ opacity: pending ? 0.55 : 1 }}>
            {page.items.length === 0 ? <Empty title={t("No explicit policies")}>{t("Everything uses the default policy.")}</Empty> : (
              <Table rows={page.items} cols={[
                { label: t("Country"), render: (r) => <a href={buildHash("policies", [r.id])}><b>{r.country}</b></a> },
                { label: t("Sender kind"), render: (r) => t(r.kind) },
                { label: t("Allowed"), render: allowedBadge },
                { label: t("Requires approval"), render: (r) => yesNo(r.requiresApproval) },
                { label: t("Revision"), render: (r) => <>r{r.revision}{!allRevisions && <> <StatusBadge>active</StatusBadge></>}</> },
                { label: t("Effective from"), hideSm: true, render: (r) => <Time value={r.effectiveFrom} /> },
                { label: "", render: (r) => <div className="row-actions"><a className="btn small" href={buildHash("policies", [r.id])}>{t("Details")}</a>{write && !allRevisions && <Button className="small" onClick={() => setDialog({ initial: r })}>{t("New revision")}</Button>}</div> },
              ]} />
            )}
            <Pagination busy={pending} page={{ offset, limit, count: page.items.length, hasMore: page.hasMore }} onChange={(o) => go({ ...base, offset: o || "" })} />
          </div>
        )}
      </Card>
      {dialog && <PolicyDialog initial={dialog.initial} onClose={() => setDialog(null)} onSaved={saved} />}
    </>
  );
}

function PolicyDetail({ id, write }) {
  const { toast } = useUi();
  const policy = useLoad(async (signal) => adaptPolicy(await api.get(`/admin/sender-policies/${id}`, {}, { signal })), [id]);
  const p = policy.data;
  const revisions = useLoad(async (signal) => (p ? (await api.page("/admin/sender-policies", { country: p.country, sender_kind: p.kind, limit: 200 }, { signal })).items.map(adaptPolicy) : null), [p?.country, p?.kind]);
  const eff = useLoad(async (signal) => (p ? fetchEffective(p.country, p.kind, signal) : null), [p?.country, p?.kind]);
  const [dialog, setDialog] = useState(false);
  const [result, setResult] = useState(null);
  const refresh = async () => { await Promise.all([revisions.reload(), eff.reload()]); };
  if (!UUID.test(id)) return null;
  const events = (revisions.data || []).slice().reverse().map((r) => ({
    id: r.id, at: r.effectiveFrom, title: t("Revision {rev}", { rev: r.revision }) + (r.id === id ? ` (${t("this one")})` : ""), status: r.allowed ? "allowed" : "denied",
    detail: [t("Requires approval: {v}", { v: yesNo(r.requiresApproval) }), r.reason].filter(Boolean).join(" · "),
  }));
  return (
    <>
      <p><a href={buildHash("policies")}>← {t("Back to Country Policies")}</a></p>
      {policy.loading && !p ? <Skeleton rows={4} /> : policy.error && !p ? <ErrorState error={policy.error} retry={policy.error.retryable ? policy.reload : undefined} /> : (
        <>
          {result && <div className="alert good" role="status"><span>{result.created ? t("Policy revision {rev} saved for {country} {kind}.", { rev: result.policy.revision, country: result.policy.country, kind: t(result.policy.kind) }) : t("No change: this is identical to the current revision, so no new revision was created.")}{result.revokedSenders > 0 && ` ${t("{n} approved sender IDs were revoked.", { n: result.revokedSenders })}`}</span></div>}
          <Card title={`${p.country} · ${t(p.kind)} · r${p.revision}`} subtitle={t("Policy revisions are immutable.")} actions={write ? <Button variant="primary" className="small" onClick={() => setDialog(true)}>{t("New revision from this")}</Button> : null}>
            <DetailList>
              <DetailRow label={t("Allowed")}>{allowedBadge(p)}</DetailRow>
              <DetailRow label={t("Requires approval")}>{yesNo(p.requiresApproval)}</DetailRow>
              <DetailRow label={t("Reason")}>{p.reason}</DetailRow>
              <DetailRow label={t("Revision")}>{p.revision}</DetailRow>
              <DetailRow label={t("Effective from")}>{p.effectiveFrom ? <Time value={p.effectiveFrom} /> : null}</DetailRow>
              <DetailRow label={t("Created by")}><span className="mono">{shortId(p.createdById)}</span></DetailRow>
              {write && <DetailRow label={t("Content hash")}><span className="mono">{p.contentHash}</span></DetailRow>}
            </DetailList>
          </Card>
          <Card title={t("Effective policy now")} subtitle={t("Calculated by the server.")}>
            {eff.error && !eff.data ? <ErrorState error={eff.error} retry={eff.reload} /> : eff.data ? <EffectiveSummary eff={eff.data} /> : <Skeleton rows={2} />}
          </Card>
          <Card title={t("Revision history")} subtitle={t("Newest first. Read-only.")}>
            {revisions.error && !revisions.data ? <ErrorState error={revisions.error} retry={revisions.reload} /> : revisions.loading && !revisions.data ? <Skeleton rows={3} /> : <AuditTimeline events={events} />}
          </Card>
          {dialog && <PolicyDialog initial={p} onClose={() => setDialog(false)} onSaved={async (r) => { setResult(r); toast(r.created ? t("Policy revision {rev} saved", { rev: r.policy.revision }) : t("No change: identical to the current revision")); await refresh(); }} />}
        </>
      )}
    </>
  );
}

export default function Policies({ write, rest = [], query = {} }) {
  if (rest[0]) return <PolicyDetail id={rest[0]} write={write} />;
  return <PolicyList query={query} write={write} />;
}
