import { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { can } from "../routes.js";
import { Button, Card, DetailList, DetailRow, Empty, ErrorBox, ErrorState, Field, FilterBar, Pagination, SearchInput, Skeleton, StatusBadge, Table, Time, displayMessage, statusText, useAction, useUi } from "../ui.jsx";
import { LIST_CAP, useSenderDetail, useSenderIds } from "./hooks.js";
import { isDriftMismatch } from "./model.js";

// Rregull i palëvizshëm i kësaj faqeje: shfaqim FUSHAT E SERVERIT (effective_status, sync_status, authority_mode, can_resubmit,
// can_review_locally). Nuk llogaritim autorizim, politikë, fituesin local/central dhe nuk ndryshojmë statusin në mënyrë optimiste.

const AUTHORITY = {
  local: T("Local"),
  shadow: T("Local (Central verification active)"),
  central: T("Central"),
};
export const authorityLabel = (mode) => (AUTHORITY[mode] ? t(AUTHORITY[mode]) : mode || "-");

const KIND = { alphanumeric: T("Name"), numeric: T("Phone number") };
export const kindLabel = (kind) => (KIND[kind] ? t(KIND[kind]) : kind || "-");

// Staf = principal pa owner_ref (llogari klienti kurrë) + leja e shqyrtimit. Serveri e rikontrollon çdo veprim.
export const isStaff = (me) => !me.owner_ref && can(me, "sender:review");

/** Statusi EFEKTIV (nga serveri) si shenjë kryesore; për stafin, rekordi lokal shfaqet si diagnostikë dytësore kur ndryshon. */
export function SenderStatus({ sender, staff }) {
  return (
    <>
      <StatusBadge>{sender.effectiveStatus}</StatusBadge>
      {sender.syncStatus === "stale" && sender.authorityMode === "central" && <> <StatusBadge>stale</StatusBadge></>}
      {staff && sender.localStatus !== sender.effectiveStatus && (
        <small className="sender-note">{t("Local record: {status}", { status: statusText(sender.localStatus) })}</small>
      )}
    </>
  );
}

export function SyncCell({ sender }) {
  if (sender.syncStatus === "not_applicable") return <span className="muted" aria-label={t("Not applicable")}>–</span>;
  return <StatusBadge>{sender.syncStatus}</StatusBadge>;
}

/** Mesazhe ndihmëse për not_synchronized / stale. Asnjë nuk pretendon miratim ose refuzim. */
export function SyncNotice({ sender, onRefresh }) {
  // Avertismenti vlen vetëm kur serveri e bën efektiv not_synchronized (autoriteti central). Në local/shadow, sync_status është diagnostikë e stafit.
  if (sender.effectiveStatus === "not_synchronized") {
    return (
      <div className="alert warn" role="status">
        <span>{t("This sender ID hasn't been confirmed yet, so it is not approved for sending. Refresh in a moment.")}</span>
        {onRefresh && <button type="button" className="link" onClick={onRefresh}>{t("Refresh")}</button>}
      </div>
    );
  }
  if (sender.syncStatus === "stale" && sender.authorityMode === "central") {
    return <div className="alert warn" role="status"><span>{t("The synchronized data is older than usual. The last known status is still shown; the server decides how it is used.")}</span></div>;
  }
  return null;
}

/** Vendimet lokale (approve/reject/revoke). Shfaqen vetëm kur serveri thotë can_review_locally; në central: shënim vetëm-lexim. */
export function SenderReviewActions({ sender, me, onDone }) {
  const { confirm, toast } = useUi();
  const [busy, setBusy] = useState(false);
  if (!isStaff(me)) return null;
  if (!sender.canReviewLocally) return <small className="muted">{t("Sender approvals are managed centrally.")}</small>;
  const decide = async (action) => {
    let body = {};
    if (action !== "approve") {
      const reason = await confirm({
        title: action === "reject" ? t("Reject — tell them why") : t("Revoke — tell them why"),
        body: t("The customer sees this. Be specific so they can fix it."), input: t("Reason"),
        placeholder: t("e.g. Brand name doesn't match your company"), inputRequired: true, minLength: 3,
        confirmLabel: action === "reject" ? t("Reject") : t("Revoke"), danger: true,
      });
      if (!reason) return;
      body = { reason };
    }
    setBusy(true);
    try {
      await api.post(`/v1/sender-ids/${sender.id}/${action}`, body, { noOwner: true });
      toast(action === "approve" ? t("Approved") : t("Decision saved"));
    } catch (e) {
      toast(displayMessage(e), e.status === 409 ? "info" : "bad"); // 409 (ngrirë / konflikt / garë): mesazh miqësor, pastaj rifreskim nga serveri
    } finally {
      setBusy(false);
      onDone && onDone(); // gjithmonë rikërko nga serveri; asnjë status lokal i shpikur
    }
  };
  const who = `${sender.value} (${sender.country})`;
  return (
    <div>
      <div className="row-actions">
        {sender.localStatus === "pending" && (
          <>
            <Button variant="primary" className="small" busy={busy} aria-label={t("Approve {sender}", { sender: who })} onClick={() => decide("approve")}>{t("Approve")}</Button>
            <Button variant="danger" className="small" busy={busy} aria-label={t("Reject {sender}", { sender: who })} onClick={() => decide("reject")}>{t("Reject")}</Button>
          </>
        )}
        {sender.localStatus === "approved" && (
          <Button variant="danger" className="small" busy={busy} aria-label={t("Revoke {sender}", { sender: who })} onClick={() => decide("revoke")}>{t("Revoke")}</Button>
        )}
      </div>
    </div>
  );
}

/** Ridërgim: vetëm kur can_resubmit=true. Pas sukseses rikërkohet lista; statusi nuk preket këtu. */
export function ResubmitButton({ sender, onDone, className = "small" }) {
  const { confirm } = useUi();
  const a = useAction();
  if (!sender.canResubmit) return null;
  const go = async () => {
    if (!(await confirm({ title: t("Submit again?"), body: t("It will go back into the review queue."), confirmLabel: t("Submit again") }))) return;
    await a.run(async () => { const r = await api.post("/v1/sender-ids", { owner_ref: sender.ownerRef, country: sender.country, value: sender.value }); if (onDone) await onDone(); return r; }, t("Submitted again"));
  };
  return (
    <>
      <Button className={className} busy={a.busy} aria-label={t("Submit {sender} again", { sender: `${sender.value} (${sender.country})` })} onClick={go}>{t("Submit again")}</Button>
      {a.error && <ErrorBox error={a.error} />}
    </>
  );
}

const COUNTRIES = [["AL", T("Albania")], ["XK", T("Kosovo")], ["MK", T("North Macedonia")], ["ME", T("Montenegro")], ["RS", T("Serbia")], ["GR", T("Greece")], ["IT", T("Italy")], ["DE", T("Germany")], ["AT", T("Austria")], ["CH", T("Switzerland")], ["FR", T("France")], ["GB", T("United Kingdom")], ["US", T("United States")], ["TR", T("Türkiye")]];

/** Formular kërkese. Validim vetëm për përdorshmëri (e zbrazët/gjatësi); rregullat e formatit i zbaton serveri dhe kthehen si gabim fushe. */
export function SenderRequestForm({ onDone, formRef }) {
  const [f, setF] = useState({ country: "AL", value: "" });
  const a = useAction();
  const valueRef = useRef(null);
  const countryRef = useRef(null);
  const err = a.error;
  const fieldError = (name) => (err && err.fields && err.fields[name]) || (name === "value" && err?.code === "invalid_sender" ? err.message : null);
  const global = err && !fieldError("value") && !fieldError("country") && !(err.fields && Object.keys(err.fields).length) ? err : null;
  const otherFields = err && err.fields ? Object.entries(err.fields).filter(([k]) => !["value", "country"].includes(k)) : [];
  // fokus te fusha e parë e pavlefshme pas një dështimi
  useEffect(() => {
    if (!err) return;
    if (fieldError("country")) countryRef.current?.focus();
    else if (fieldError("value")) valueRef.current?.focus();
  }, [err]); // eslint-disable-line react-hooks/exhaustive-deps
  const edit = (patch) => { setF((s) => ({ ...s, ...patch })); if (err) a.clear(); };
  const submit = async (e) => {
    e.preventDefault();
    // rikërkohet lista nga serveri PARA njoftimit të suksesit (asnjë rresht i shpikur lokalisht)
    const ok = await a.run(async () => { const r = await api.post("/v1/sender-ids", { country: f.country, value: f.value.trim() }); if (onDone) await onDone(); return r; }, t("Request sent for review"));
    if (ok !== undefined) setF((s) => ({ ...s, value: "" }));
  };
  const tooShort = f.value.trim().length > 0 && f.value.trim().length < 3;
  return (
    <Card title={t("Request a sender ID")} id="sender-request">
      <form className="form" onSubmit={submit} noValidate ref={formRef}>
        <div className="grid two">
          <Field label={t("Country it will send to")} error={fieldError("country")}>
            <select ref={countryRef} value={f.country} aria-invalid={!!fieldError("country")} onChange={(e) => edit({ country: e.target.value })}>
              {COUNTRIES.map(([c, n]) => <option key={c} value={c}>{t(n)} ({c})</option>)}
            </select>
          </Field>
          <Field label={t("Sender ID")} hint={t("For example your brand name")} error={fieldError("value") || (tooShort ? t("Use at least 3 characters") : null)}>
            <input ref={valueRef} required maxLength={16} value={f.value} aria-invalid={!!(fieldError("value") || tooShort)} onChange={(e) => edit({ value: e.target.value })} placeholder="ACME" />
          </Field>
        </div>
        {otherFields.map(([k, m]) => <div key={k} className="alert bad" role="alert">{k}: {m}</div>)}
        <ErrorBox error={global} />
        <div><Button variant="primary" busy={a.busy} disabled={f.value.trim().length < 3}>{t("Submit for approval")}</Button></div>
      </form>
    </Card>
  );
}

const PAGE_SIZE = 20;
const STATUS_FILTERS = ["approved", "pending", "rejected", "revoked", "not_synchronized", "policy_denied"];

export function SenderList({ me }) {
  const staff = isStaff(me);
  const list = useSenderIds({}, 15000);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [country, setCountry] = useState("");
  const [kind, setKind] = useState("");
  const [offset, setOffset] = useState(0);
  const formRef = useRef(null);
  const rows = list.data || [];
  const filtering = !!(q || status || country || kind);
  const norm = q.trim().toLowerCase();
  // Filtrim vetëm mbi rreshtat e ngarkuar (serveri kufizon në LIST_CAP); asnjë logjikë autorizimi.
  const shown = rows.filter((r) => (!status || r.effectiveStatus === status) && (!country || r.country === country) && (!kind || r.kind === kind) && (!norm || r.value.toLowerCase().includes(norm)));
  const pageRows = shown.slice(offset, offset + PAGE_SIZE);
  const countries = [...new Set(rows.map((r) => r.country).filter(Boolean))].sort();
  const reset = () => { setQ(""); setStatus(""); setCountry(""); setKind(""); setOffset(0); };
  const setF = (fn) => (v) => { fn(v); setOffset(0); };
  const canRequest = can(me, "sender:request");
  const central = rows.some((r) => r.authorityMode === "central");
  const focusRequest = () => { document.getElementById("sender-request")?.scrollIntoView?.({ behavior: "smooth" }); formRef.current?.querySelector("input")?.focus(); };
  const refresh = () => list.reload();

  return (
    <>
      <div className="help"><b>{t("What is a sender ID?")}</b> {t("It's the name people see instead of a phone number, like ACME. Use 3–11 letters or digits (at least one letter), or a phone number. Each country needs its own approval, and only your brand or business names are approved.")}</div>
      {central && <div className="alert warn" role="status"><span>{t("Sender approvals are managed centrally.")}</span></div>}
      {canRequest && <SenderRequestForm formRef={formRef} onDone={refresh} />}
      <Card title={t("Your sender IDs")} actions={<Button className="small" onClick={refresh} aria-label={t("Refresh")}>{t("Refresh")}</Button>}>
        {list.error && !list.data ? <ErrorState error={list.error} retry={list.reload} /> : (
          <>
            {list.stale && <ErrorState stale error={list.error} retry={list.reload} />}
            <FilterBar label={t("Filter sender IDs")}>
              <SearchInput label={t("Search sender IDs")} placeholder={t("Search sender IDs")} value={q} onChange={(e) => setF(setQ)(e.target.value)} />
              <select aria-label={t("Status")} value={status} onChange={(e) => setF(setStatus)(e.target.value)}>
                <option value="">{t("All statuses")}</option>
                {STATUS_FILTERS.map((s) => <option key={s} value={s}>{statusText(s)}</option>)}
              </select>
              <select aria-label={t("Country")} value={country} onChange={(e) => setF(setCountry)(e.target.value)}>
                <option value="">{t("All countries")}</option>
                {countries.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              <select aria-label={t("Type")} value={kind} onChange={(e) => setF(setKind)(e.target.value)}>
                <option value="">{t("All types")}</option>
                <option value="alphanumeric">{kindLabel("alphanumeric")}</option>
                <option value="numeric">{kindLabel("numeric")}</option>
              </select>
            </FilterBar>
            {rows.length >= LIST_CAP && <small className="muted" role="status">{t("Showing the newest {n} sender IDs. Filters apply to these.", { n: LIST_CAP })}</small>}
            {list.loading && !list.data ? <Skeleton rows={4} /> : rows.length === 0 ? (
              <Empty title={t("No sender IDs yet")} action={canRequest && <Button variant="primary" onClick={focusRequest}>{t("Request a sender ID")}</Button>}>
                {canRequest ? t("Request one above. You can't send SMS until one is approved.") : t("Nothing here yet.")}
              </Empty>
            ) : shown.length === 0 ? (
              <Empty title={t("No sender IDs match your filters")} action={<Button onClick={reset}>{t("Clear filters")}</Button>}>{t("Try a different search or clear the filters.")}</Empty>
            ) : (
              <>
                <Table rows={pageRows} cols={[
                  { label: t("Sender"), render: (r) => <a href={`#senders/${r.id}`} aria-label={t("Details for {sender}", { sender: `${r.value} (${r.country})` })}><b>{r.value}</b></a> },
                  { label: t("Country"), key: "country" },
                  { label: t("Type"), hideSm: true, render: (r) => kindLabel(r.kind) },
                  { label: t("Status"), render: (r) => <SenderStatus sender={r} staff={staff} /> },
                  { label: t("Sync"), hideSm: true, render: (r) => <SyncCell sender={r} /> },
                  { label: t("Authority"), hideSm: true, render: (r) => authorityLabel(r.authorityMode) },
                  { label: t("Requested"), hideSm: true, render: (r) => <Time value={r.createdAt} /> },
                  { label: "", render: (r) => <div className="row-actions"><a className="btn small" href={`#senders/${r.id}`} aria-label={t("Details for {sender}", { sender: `${r.value} (${r.country})` })}>{t("Details")}</a><ResubmitButton sender={r} onDone={refresh} /></div> },
                ]} />
                <Pagination page={{ offset, limit: PAGE_SIZE, count: pageRows.length, hasMore: offset + PAGE_SIZE < shown.length }} onChange={setOffset} />
              </>
            )}
            {filtering && shown.length > 0 && <small className="muted" role="status">{t("{n} of {total} shown", { n: shown.length, total: rows.length })}</small>}
          </>
        )}
      </Card>
    </>
  );
}

export function SenderDetail({ me, id }) {
  const staff = isStaff(me);
  const d = useSenderDetail(id);
  const s = d.data;
  return (
    <>
      <p><a href="#senders">← {t("Back to sender IDs")}</a></p>
      {d.loading && !s ? <Skeleton rows={5} /> : d.error && !s ? <ErrorState error={d.error} retry={d.reload} /> : (
        <>
          {d.stale && <ErrorState stale error={d.error} retry={d.reload} />}
          <SyncNotice sender={s} onRefresh={d.reload} />
          <Card title={s.value} subtitle={`${s.country} · ${kindLabel(s.kind)}`} actions={<><ResubmitButton sender={s} onDone={d.reload} /><SenderReviewActions sender={s} me={me} onDone={d.reload} /></>}>
            <DetailList>
              <DetailRow label={t("Status")}><SenderStatus sender={s} staff={false} /></DetailRow>
              <DetailRow label={t("Authority")}>{authorityLabel(s.authorityMode)}</DetailRow>
              <DetailRow label={t("Sync")}><SyncCell sender={s} /></DetailRow>
              {s.reason && <DetailRow label={t("Reason")}>{s.reason}</DetailRow>}
              <DetailRow label={t("Requested")}>{s.createdAt ? <Time value={s.createdAt} /> : null}</DetailRow>
              {staff && s.reviewedBy && <DetailRow label={t("Reviewed by")}>{s.reviewedBy}</DetailRow>}
            </DetailList>
          </Card>
          {staff && (
            <Card title={t("Diagnostics")} subtitle={t("Visible to staff only.")}>
              <DetailList>
                <DetailRow label={t("Local status")}><StatusBadge>{s.localStatus}</StatusBadge></DetailRow>
                <DetailRow label={t("Central status")}>{s.centralStatus ? <StatusBadge>{s.centralStatus}</StatusBadge> : null}</DetailRow>
                <DetailRow label={t("Drift")}>{s.drift ? <><StatusBadge>{isDriftMismatch(s.drift) ? "failed" : "ok"}</StatusBadge> <code>{s.drift}</code></> : null}</DetailRow>
                <DetailRow label={t("Control-plane revision")}>{s.cpRevision}</DetailRow>
                <DetailRow label={t("Policy revision")}>{s.policyRevision}</DetailRow>
                {s.bootstrapIssues.length > 0 && <DetailRow label={t("Bootstrap issues")}>{s.bootstrapIssues.join(", ")}</DetailRow>}
              </DetailList>
            </Card>
          )}
          <small className="muted">{t("Decision history isn't available here yet.")}</small>
        </>
      )}
    </>
  );
}
