import { useState } from "react";
import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { UUID, buildHash } from "../routes.js";
import { Button, Card, DetailList, DetailRow, Empty, ErrorState as BaseErrorState, FilterBar, Modal, Pagination, Skeleton, StatusBadge, Table, Tabs, useLoad } from "../ui.jsx";
import { accountingDate, exactUtc, formatMoney, level } from "./format.js";

// Pamje financiare: mesazhi i papërpunuar i serverit shfaqet vetëm për gabimet e kuruara (konflikt/validim, ose me kod të njohur).
// 403/404/429/5xx marrin tekst të përgjithshëm; asnjë stack trace, payload ofruesi apo tekst i brendshëm nuk del në ekran.
const GENERIC = {
  forbidden: T("You don't have access to this information."), not_found: T("We couldn't find that. It may have been removed."),
  rate_limited: T("Too many requests. Wait a moment and try again."), server: T("Something went wrong on our side. Try again in a moment."), other: T("The server couldn't complete this request."),
};
export function safeError(e) {
  if (!e || e.friendly || ["network", "unauthenticated", "conflict", "validation"].includes(e.kind)) return e;
  const safe = Object.create(e);
  Object.defineProperty(safe, "message", { value: t(GENERIC[e.kind] || GENERIC.other) });
  return safe;
}
export const ErrorState = ({ error, ...p }) => <BaseErrorState error={safeError(error)} {...p} />;

export const SIZES = [25, 50, 100, 200];
export const POLL_MS = 45000;

export const Money = ({ value, currency }) => <span className="mono-num">{formatMoney(value, currency)}</span>;
export const Ts = ({ value }) => (value ? <time dateTime={value} title={exactUtc(value)}>{exactUtc(value)}</time> : <span>-</span>);
export const DateOnly = ({ value }) => (value ? <time dateTime={value} title={exactUtc(value)}>{accountingDate(value)}</time> : <span>-</span>);
export const shortId = (id) => (id ? `${String(id).slice(0, 8)}…` : "-");
export const Level = ({ value }) => <StatusBadge>{level(value)}</StatusBadge>;
export const humanize = (s) => String(s ?? "").replace(/_/g, " ");

export const EntLink = ({ id, route = "com-invoices", extra = {} }) =>
  id ? <a className="mono" href={buildHash(route, [], { ...extra, enterprise_id: id })} title={id} aria-label={t("Enterprise {id}", { id })}>{shortId(id)}</a> : <span>-</span>;

export const Actor = ({ a }) => (a ? <span>{a.label || shortId(a.user_id)}</span> : <span>-</span>);

/** Skeda aktive nga URL-ja (#route?tab=x); tab i panjohur ⇒ i pari. */
export function TabBar({ route, tabs, query }) {
  const active = tabs.some((x) => x.id === query.tab) ? query.tab : tabs[0].id;
  return { active, bar: <Tabs tabs={tabs} value={active} onChange={(id) => { location.hash = buildHash(route, [], id === tabs[0].id ? {} : { tab: id }); }} /> };
}

/**
 * Listë e faqosur nga serveri me filtra në URL. Asnjë faqosje në klient: çdo faqe është një kërkesë.
 * `filters`: [{ name, label, options?, type?, maxLength?, upper? }] — pa options ⇒ input (text|date); emrat me `_id` kërkojnë UUID.
 */
export function ServerList({ route, rest = [], tab, query, path, filters = [], params = {}, cols, title, emptyTitle, emptyText, poll = 0 }) {
  const limit = SIZES.includes(Number(query.limit)) ? Number(query.limit) : 50;
  const offset = Math.max(0, Number.parseInt(query.offset, 10) || 0);
  const applied = Object.fromEntries(filters.map((f) => [f.name, query[f.name] || ""]));
  const base = tab ? { tab } : {};
  const go = (q) => { location.hash = buildHash(route, rest, { ...base, ...q }); };
  const key = JSON.stringify({ path, limit, offset, applied, params });
  const list = useLoad(async (signal) => ({ key, page: await api.page(path, { limit, offset, ...applied, ...params }, { signal }) }), [key], poll);
  const [draft, setDraft] = useState(applied);
  const [bad, setBad] = useState({});
  const fieldErrors = list.error?.kind === "validation" ? list.error.fields || {} : {};
  const page = list.data?.page;
  const pending = list.data && list.data.key !== key;
  const filtering = Object.values(applied).some(Boolean);
  const apply = (e) => {
    e.preventDefault();
    const errs = {};
    const clean = {};
    for (const f of filters) {
      const v = (draft[f.name] || "").trim();
      if (v && f.name.endsWith("_id") && !UUID.test(v)) errs[f.name] = t("Enter a valid ID (UUID).");
      clean[f.name] = f.upper ? v.toUpperCase() : v;
    }
    // Intervali i datave: gabim i dukshëm, kurrë këmbim i heshtur (serveri e refuzon po ashtu me 422)
    if (clean.issued_from && clean.issued_to && clean.issued_from > clean.issued_to) errs.issued_to = t("The end date must not be before the start date.");
    setBad(errs);
    if (Object.keys(errs).length === 0) go({ ...clean, limit: limit === 50 ? "" : limit });
  };
  const clear = () => { setDraft(Object.fromEntries(filters.map((f) => [f.name, ""]))); setBad({}); go({}); };
  return (
    <Card title={title}>
      <form onSubmit={apply}>
        <FilterBar label={t("Filters")}>
          {filters.map((f) => f.options ? (
            <select key={f.name} aria-label={f.label} value={draft[f.name] || ""} onChange={(e) => setDraft({ ...draft, [f.name]: e.target.value })}>
              <option value="">{t("All")}</option>{f.options.map((o) => <option key={o} value={o}>{t(o)}</option>)}
            </select>
          ) : (
            <input key={f.name} type={f.type || "text"} aria-label={f.label} placeholder={f.label} maxLength={f.maxLength} aria-invalid={bad[f.name] || fieldErrors[f.name] ? true : undefined}
              value={draft[f.name] || ""} onChange={(e) => setDraft({ ...draft, [f.name]: e.target.value })} />
          ))}
          {filters.length > 0 && <Button variant="primary">{t("Apply filters")}</Button>}
          {filtering && <Button type="button" onClick={clear}>{t("Clear filters")}</Button>}
          <select aria-label={t("Rows per page")} value={limit} onChange={(e) => go({ ...applied, limit: e.target.value === "50" ? "" : e.target.value })}>
            {SIZES.map((n) => <option key={n} value={n}>{t("{n} per page", { n })}</option>)}
          </select>
        </FilterBar>
        {Object.keys({ ...bad, ...fieldErrors }).length > 0 && (
          <div className="alert warn" role="alert">{Object.entries({ ...fieldErrors, ...bad }).map(([k, msg]) => <div key={k}>{filters.find((f) => f.name === k)?.label || k}: {msg}</div>)}</div>
        )}
      </form>
      {list.error && !page ? <ErrorState error={list.error} retry={list.reload} /> : list.loading && !page ? <Skeleton rows={5} /> : (
        <div aria-busy={pending || undefined} style={{ opacity: pending ? 0.55 : 1 }}>
          {list.stale && <ErrorState stale error={list.error} retry={list.reload} />}
          {page.items.length === 0 ? (
            offset > 0 ? <Empty title={t("No more results")} action={<Button onClick={() => go(applied)}>{t("Back to first page")}</Button>}>{t("This page is past the end of the list.")}</Empty>
              : filtering ? <Empty title={t("Nothing matches your filters")} action={<Button onClick={clear}>{t("Clear filters")}</Button>}>{t("Try different filters.")}</Empty>
                : <Empty title={emptyTitle}>{emptyText}</Empty>
          ) : <Table rows={page.items} cols={cols} />}
          <Pagination busy={pending} page={{ offset, limit, count: page.items.length, hasMore: page.hasMore }} onChange={(o) => go({ ...applied, limit: query.limit, offset: o || "" })} />
        </div>
      )}
    </Card>
  );
}

/** Detaj vetëm-lexim në modal: çdo fushë vjen nga rreshti i listës (asnjë kërkesë shtesë, asnjë veprim). */
export function DetailModal({ title, rows, onClose }) {
  return (
    <Modal title={title} onClose={onClose}>
      <DetailList>{rows.map(([label, value]) => <DetailRow key={label} label={label}>{value}</DetailRow>)}</DetailList>
      <div className="row end"><Button onClick={onClose}>{t("Close")}</Button></div>
    </Modal>
  );
}

/** Kartë me ngarkim vetëm-lexim për një burim; gabimi i një karte nuk prish të tjerat. */
export function Panel({ title, state, children, subtitle, actions }) {
  return (
    <Card title={title} subtitle={subtitle} actions={actions}>
      {state.error && !state.data ? <ErrorState error={state.error} retry={state.reload} /> : !state.data ? <Skeleton rows={3} /> : (
        <>{state.stale && <ErrorState stale error={state.error} retry={state.reload} />}{children(state.data)}</>
      )}
    </Card>
  );
}

export const KV = ({ rows }) => <DetailList>{rows.filter(Boolean).map(([l, v]) => <DetailRow key={l} label={l}>{v}</DetailRow>)}</DetailList>;
export const Note = ({ children }) => <p className="muted small">{children}</p>;
