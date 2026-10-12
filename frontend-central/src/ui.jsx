import { Component, createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { T, locale, t } from "./i18n.jsx";

/* ---------- Njoftime (toast) dhe dialog konfirmimi ---------- */
const Ctx = createContext(null);
export const useUi = () => useContext(Ctx);

export function UiProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const [dialog, setDialog] = useState(null);
  const id = useRef(0);

  const toast = useCallback((message, kind = "good") => {
    const n = ++id.current;
    setToasts((list) => [...list, { id: n, message, kind }]);
    setTimeout(() => setToasts((list) => list.filter((x) => x.id !== n)), kind === "bad" ? 7000 : 3800);
  }, []);
  const confirm = useCallback((opts) => new Promise((resolve) => setDialog({ ...opts, resolve })), []);
  const close = (v) => { dialog.resolve(v); setDialog(null); };

  return (
    <Ctx.Provider value={{ toast, confirm }}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((x) => (
          <div key={x.id} className={`toast ${x.kind}`}>
            <span>{x.kind === "good" ? "✓" : x.kind === "bad" ? "!" : "i"}</span>{x.message}
            <button aria-label={t("Dismiss")} onClick={() => setToasts((l) => l.filter((y) => y.id !== x.id))}>×</button>
          </div>
        ))}
      </div>
      {dialog && <Dialog dialog={dialog} close={close} />}
    </Ctx.Provider>
  );
}

function Dialog({ dialog, close }) {
  const ref = useRef(null);
  const [text, setText] = useState("");
  useEffect(() => {
    const prev = document.activeElement;
    ref.current?.querySelector("[data-autofocus]")?.focus();
    const onKey = (e) => {
      if (e.key === "Escape") return close(false);
      if (e.key !== "Tab") return;
      const f = [...(ref.current?.querySelectorAll("button:not([disabled]), input, select, textarea, [href]") || [])];
      if (!f.length) return;
      const [first, last] = [f[0], f[f.length - 1]];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    addEventListener("keydown", onKey);
    return () => { removeEventListener("keydown", onKey); prev?.focus?.(); };
  }, []); // eslint-disable-line
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && close(false)}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title" ref={ref}>
        <h3 id="dlg-title">{dialog.title}</h3>
        {dialog.body && <p className="muted">{dialog.body}</p>}
        {dialog.input && (
          <label className="field"><span>{dialog.input}</span>
            <input data-autofocus value={text} onChange={(e) => setText(e.target.value)} placeholder={dialog.placeholder || ""} /></label>
        )}
        <div className="row end">
          <button className="btn" onClick={() => close(false)}>{t("Cancel")}</button>
          <button className={`btn ${dialog.danger ? "danger-solid" : "primary"}`} data-autofocus={!dialog.input || undefined}
            disabled={dialog.input && dialog.inputRequired && text.trim().length < (dialog.minLength || 1)}
            onClick={() => close(dialog.input ? text.trim() : true)}>{dialog.confirmLabel || t("Confirm")}</button>
        </div>
      </div>
    </div>
  );
}

// Modal i përgjithshëm (formular brenda): kapje fokusi, Esc, fokus fillestar te [data-autofocus], rikthim i fokusit.
export function Modal({ title, onClose, children, busy = false }) {
  const ref = useRef(null);
  useEffect(() => {
    const prev = document.activeElement;
    (ref.current?.querySelector("[data-autofocus]") || ref.current?.querySelector("input, textarea, select, button"))?.focus();
    const onKey = (e) => {
      if (e.key === "Escape") return !busy && onClose();
      if (e.key !== "Tab") return;
      const f = [...(ref.current?.querySelectorAll("button:not([disabled]), input:not([disabled]), select, textarea, [href]") || [])];
      if (!f.length) return;
      const [first, last] = [f[0], f[f.length - 1]];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    addEventListener("keydown", onKey);
    return () => { removeEventListener("keydown", onKey); prev?.focus?.(); };
  }, []); // eslint-disable-line
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && !busy && onClose()}>
      <div className="dialog wide" role="dialog" aria-modal="true" aria-labelledby="modal-title" ref={ref}>
        <h3 id="modal-title">{title}</h3>
        {children}
      </div>
    </div>
  );
}

/* ---------- Ngarkim i të dhënave ---------- */
// `fn(signal)`: kalo `signal` te api.get(..., { signal }) që kërkesa e vjetër të anulohet kur ndryshon filtri
// ose del faqja. Përgjigjet e vjetra nuk mbishkruajnë të reja. `stale` = ka të dhëna të vjetra + gabim të fundit.
export function useLoad(fn, deps = [], refreshMs = 0) {
  const [state, set] = useState({ data: null, error: null, loading: true });
  const seq = useRef(0);
  const ctl = useRef(null);
  const load = useCallback(async () => {
    const me = ++seq.current;
    ctl.current?.abort();
    const c = (ctl.current = new AbortController());
    try {
      const data = await fn(c.signal);
      if (me === seq.current) set({ data, error: null, loading: false });
    } catch (e) {
      if (me === seq.current && !c.signal.aborted) set((s) => ({ data: s.data, error: e, loading: false }));
    }
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    set((s) => ({ ...s, loading: s.data === null }));
    load();
    const timer = refreshMs ? setInterval(() => !document.hidden && load(), refreshMs) : null;
    return () => { if (timer) clearInterval(timer); seq.current++; ctl.current?.abort(); };
  }, [load, refreshMs]);
  return { ...state, stale: !!(state.error && state.data !== null), reload: load };
}

// Ekzekuton një veprim: "duke punuar", toast suksesi, gabim i lexueshëm pranë formularit.
export function useAction() {
  const { toast } = useUi();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const run = async (fn, okMsg) => {
    setBusy(true); setError(null);
    try { const r = await fn(); if (okMsg) toast(okMsg); return r === undefined ? true : r; }
    catch (e) { setError(e); if (e.status === 0 || e.status >= 500) toast(displayMessage(e), "bad"); return undefined; }
    finally { setBusy(false); }
  };
  return { busy, error, ok: "", run, clear: () => setError(null) };
}

/* ---------- Komponentë bazë ---------- */
export const Card = ({ title, subtitle, actions, children, className = "", id }) => (
  <section className={`card ${className}`} id={id}>
    {(title || actions) && (
      <header className="card-h">
        <div><h3>{title}</h3>{subtitle && <p className="sub">{subtitle}</p>}</div>
        <div className="row wrap">{actions}</div>
      </header>
    )}
    {children}
  </section>
);

export const Stat = ({ label, value, sub, tone }) => (
  <div className={`stat ${tone || ""}`}>
    <div className="stat-l">{label}</div>
    <div className="stat-v">{value}</div>
    {sub && <div className="stat-s">{sub}</div>}
  </div>
);

// Sistemi i vetëm semantik i statuseve: faqet NUK zgjedhin ngjyra; kalojnë statusin e serverit te <StatusBadge>.
// Kuptimi nuk varet vetëm nga ngjyra: çdo ton ka simbol dhe teksti i statusit shfaqet gjithmonë.
export const STATUS_TONES = {
  delivered: "good", completed: "good", verified: "good", active: "good", succeeded: "good", ok: "good", approved: "good", paid: "good", confirmed: "good", published: "good", synchronized: "good", allowed: "good", pass: "good", warn: "warn", fail: "bad", critical: "bad", warning: "warn",
  sent: "info", running: "info", queued: "info", scheduled: "info", preparing: "info", sending: "info", open: "info", processing: "info",
  pending: "warn", paused: "warn", suspended: "warn", unpaid: "warn", not_synchronized: "warn", stale: "warn", partially_settled: "warn", scheduled_cancel: "warn", reconciliation_required: "bad",
  settled: "good", invoiced: "good", credited: "info", reversed: "muted", no_charge: "muted", unknown: "muted",
  draft: "muted", cancelled: "muted", skipped: "muted", expired: "muted", void: "muted", retired: "muted", not_applicable: "muted",
  failed: "bad", bounced: "bad", complained: "bad", disabled: "bad", opted_out: "bad", rejected: "bad", revoked: "bad", overdue: "bad", policy_denied: "bad", denied: "bad", explicit: "info", default: "muted",
};
const TONES = STATUS_TONES;
export const statusTone = (status) => TONES[String(status).toLowerCase()] || "muted";
const GLYPH = { good: "✓", info: "●", warn: "◔", bad: "✕", muted: "–" };
// Teksti i statusit: përkthimi shqip, ose (anglisht/pa përkthim) çelësi pa "_": "not_synchronized" → "not synchronized".
export const statusText = (status) => { const key = String(status).toLowerCase(); const label = t(key); return label === key ? key.replace(/_/g, " ") : label; };
export const StatusBadge = ({ children }) => {
  const tone = statusTone(children);
  return <span className={`badge ${tone}`}><i aria-hidden>{GLYPH[tone]}</i>{statusText(children)}</span>;
};
export const Badge = StatusBadge; // emri i vjetër (përdoret nga faqet ekzistuese)

export const Button = ({ variant = "", busy, children, ...p }) => (
  <button {...p} disabled={p.disabled || busy} className={`btn ${variant} ${p.className || ""}`}>
    {busy && <span className="spin" aria-hidden />}{children}
  </button>
);

export const Field = ({ label, hint, error, children }) => (
  <label className={`field ${error ? "has-error" : ""}`}>
    <span>{label}</span>
    {children}
    {error ? <small className="err">{error}</small> : hint && <small>{hint}</small>}
  </label>
);

export const Empty = ({ icon = "∅", title, children, action }) => (
  <div className="empty">
    <div className="empty-i">{icon}</div>
    {title && <b>{title}</b>}
    <div>{children}</div>
    {action}
  </div>
);

// Teksti i gabimit për përdoruesin. 5xx pa kod të njohur NUK shfaq tekstin e serverit (mund të ketë detaje të brendshme).
export function displayMessage(error) {
  if (!error) return "";
  if (error.kind === "server" && !error.friendly) return t("Something went wrong on our side. Try again in a moment.");
  return error.message || String(error);
}

const ERROR_TITLES = {
  network: T("You seem to be offline"), unauthenticated: T("Your session has ended"), forbidden: T("You don't have access"),
  not_found: T("We couldn't find that"), conflict: T("This changed in the meantime"), rate_limited: T("Too many requests"),
  validation: T("Check the details"), server: T("Something went wrong"),
};
export const errorTitle = (error) => t(ERROR_TITLES[error?.kind] || T("Something went wrong"));

export const ErrorBox = ({ error, retry }) =>
  error ? (
    <div className="alert bad" role="alert">
      <span>{displayMessage(error)}</span>
      {retry && <button className="link" onClick={retry}>{t("Try again")}</button>}
    </div>
  ) : null;

// Gabim në nivel faqeje/blloku (jo brenda formularit): titull sipas llojit + mesazh + "Provo përsëri" kur ka kuptim.
export function ErrorState({ error, retry, stale = false }) {
  if (!error) return null;
  const canRetry = retry && (error.retryable ?? true);
  const wait = error.retryAfter ? t("Wait {n} s before trying again.", { n: error.retryAfter }) : "";
  return (
    <div className={`errorstate ${stale ? "stale" : ""}`} role="alert">
      <div className="empty-i" aria-hidden>!</div>
      <b>{errorTitle(error)}</b>
      <div className="muted">{displayMessage(error)} {wait}</div>
      {stale && <small className="muted">{t("Showing the last data we received.")}</small>}
      {canRetry && <Button onClick={retry}>{t("Try again")}</Button>}
    </div>
  );
}

// Kap gabimet e renderimit (p.sh. një faqe me defekt) që UI-ja të mos bëhet e bardhë.
export class ErrorBoundary extends Component {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidUpdate(prev) { if (this.state.failed && prev.resetKey !== this.props.resetKey) this.setState({ failed: false }); }
  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="errorstate" role="alert">
        <div className="empty-i" aria-hidden>!</div>
        <b>{t("Something went wrong")}</b>
        <div className="muted">{t("This page hit an unexpected problem. Reload it or go back to the overview.")}</div>
        <div className="row"><Button onClick={() => location.reload()}>{t("Reload")}</Button><a className="btn" href="#dashboard">{t("Overview")}</a></div>
      </div>
    );
  }
}
export const Notice = ({ error }) => <ErrorBox error={error} />;

export const Skeleton = ({ rows = 3 }) => (
  <div className="skeleton" aria-busy="true" aria-label={t("Loading")}>
    {Array.from({ length: rows }, (_, i) => <div key={i} style={{ width: `${92 - i * 11}%` }} />)}
  </div>
);

export function Table({ cols, rows, empty = t("Nothing here yet."), loading, emptyTitle, emptyAction, footer }) {
  if (loading && !rows?.length) return <Skeleton rows={4} />;
  if (!rows || rows.length === 0) return <Empty title={emptyTitle} action={emptyAction}>{empty}</Empty>;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{cols.map((c) => <th key={c.key || c.label} className={`${c.num ? "num" : ""} ${c.hideSm ? "hide-sm" : ""}`.trim()}>{c.label}</th>)}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.id ?? i} onClick={r._onClick} className={r._onClick ? "click" : ""} tabIndex={r._onClick ? 0 : undefined}
              onKeyDown={r._onClick ? (e) => e.key === "Enter" && r._onClick() : undefined}>
              {cols.map((c) => <td key={c.key || c.label} className={`${c.num ? "num" : ""} ${c.hideSm ? "hide-sm" : ""}`.trim()} data-label={c.label}>{c.render ? c.render(r) : r[c.key]}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      {footer}
    </div>
  );
}

export function Tabs({ tabs, value, onChange }) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((tab) => (
        <button key={tab.id} role="tab" aria-selected={value === tab.id} className={value === tab.id ? "on" : ""} onClick={() => onChange(tab.id)}>
          {tab.label}{tab.count ? <span className="count">{tab.count}</span> : null}
        </button>
      ))}
    </div>
  );
}

export const MetricCard = Stat;
export const FormField = Field;

export function PageHeader({ title, desc, crumbs, actions, lead }) {
  return (
    <header className="top">
      {lead}
      <div className="top-t">
        {crumbs && crumbs.length > 1 && <Breadcrumbs items={crumbs} />}
        <h1>{title}</h1>{desc && <p className="muted">{desc}</p>}
      </div>
      {actions}
    </header>
  );
}

export function Breadcrumbs({ items }) {
  return (
    <nav className="crumbs" aria-label={t("Breadcrumb")}>
      <ol>
        {items.map((c, i) => (
          <li key={i} {...(i === items.length - 1 ? { "aria-current": "page" } : {})}>
            {c.href && i < items.length - 1 ? <a href={c.href}>{c.label}</a> : c.label}
          </li>
        ))}
      </ol>
    </nav>
  );
}

export const FilterBar = ({ label, children }) => <div className="toolbar" role="search" aria-label={label || t("Filters")}>{children}</div>;
export const SearchInput = ({ label, ...p }) => <input type="search" aria-label={label || t("Search")} {...p} />;

// Faqosje me zhvendosje (offset) mbi { items, hasMore, limit, offset } nga api.page().
export function Pagination({ page, onChange, busy }) {
  if (!page || (page.offset === 0 && !page.hasMore)) return null;
  const from = page.offset + 1;
  const to = page.offset + (page.count ?? page.limit);
  return (
    <nav className="pager" aria-label={t("Pagination")}>
      <Button disabled={busy || page.offset === 0} onClick={() => onChange(Math.max(0, page.offset - page.limit))}>{t("Previous")}</Button>
      <span className="muted" aria-live="polite">{t("{from}–{to}", { from, to })}</span>
      <Button disabled={busy || !page.hasMore} onClick={() => onChange(page.offset + page.limit)}>{t("Next")}</Button>
    </nav>
  );
}

export const DetailList = ({ children }) => <dl className="kvlist">{children}</dl>;
export const DetailRow = ({ label, children }) => (<><dt>{label}</dt><dd>{children ?? "-"}</dd></>);

// events: [{ id, at, title, actor?, detail?, status? }] (më i riu i pari ose i fundit, sipas thirrësit).
export function AuditTimeline({ events, empty = t("No history yet.") }) {
  if (!events || events.length === 0) return <Empty>{empty}</Empty>;
  return (
    <ol className="timeline">
      {events.map((e, i) => (
        <li key={e.id ?? i}>
          <div className="row wrap"><b>{e.title}</b>{e.status && <StatusBadge>{e.status}</StatusBadge>}<span className="muted small"><Time value={e.at} />{e.actor ? ` · ${e.actor}` : ""}</span></div>
          {e.detail && <div className="muted">{e.detail}</div>}
        </li>
      ))}
    </ol>
  );
}

export const money = (v) => (v == null ? "-" : Number(v).toLocaleString(locale(), { minimumFractionDigits: 2, maximumFractionDigits: 4 }));
export const when = (v) => (v ? new Date(v).toLocaleString(locale()) : "-");
export const dateOnly = (v) => (v ? new Date(v).toLocaleDateString(locale()) : "-");
export function ago(v) {
  if (!v) return "-";
  const s = Math.max(0, (Date.now() - new Date(v).getTime()) / 1000);
  if (s < 45) return t("just now");
  if (s < 3600) return t("{n} min ago", { n: Math.round(s / 60) });
  if (s < 86400) return t("{n} h ago", { n: Math.round(s / 3600) });
  if (s < 86400 * 14) return t("{n} d ago", { n: Math.round(s / 86400) });
  return dateOnly(v);
}
export const Time = ({ value }) => <time title={when(value)} dateTime={value}>{ago(value)}</time>;

export function useDebounced(value, ms = 350) {
  const [v, setV] = useState(value);
  useEffect(() => { const timer = setTimeout(() => setV(value), ms); return () => clearTimeout(timer); }, [value, ms]);
  return v;
}
