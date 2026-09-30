import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { locale, t } from "./i18n.jsx";

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
    const onKey = (e) => e.key === "Escape" && close(false);
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

/* ---------- Ngarkim i të dhënave ---------- */
export function useLoad(fn, deps = [], refreshMs = 0) {
  const [state, set] = useState({ data: null, error: null, loading: true });
  const load = useCallback(async () => {
    try {
      const data = await fn();
      set({ data, error: null, loading: false });
    } catch (e) {
      set((s) => ({ data: s.data, error: e, loading: false }));
    }
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    set((s) => ({ ...s, loading: s.data === null }));
    load();
    if (!refreshMs) return;
    const timer = setInterval(() => !document.hidden && load(), refreshMs);
    return () => clearInterval(timer);
  }, [load, refreshMs]);
  return { ...state, reload: load };
}

// Ekzekuton një veprim: "duke punuar", toast suksesi, gabim i lexueshëm pranë formularit.
export function useAction() {
  const { toast } = useUi();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const run = async (fn, okMsg) => {
    setBusy(true); setError(null);
    try { const r = await fn(); if (okMsg) toast(okMsg); return r === undefined ? true : r; }
    catch (e) { setError(e); if (e.status === 0 || e.status >= 500) toast(e.message, "bad"); return undefined; }
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

const TONES = {
  delivered: "good", completed: "good", verified: "good", active: "good", succeeded: "good", ok: "good", approved: "good", paid: "good", confirmed: "good", published: "good",
  sent: "info", running: "info", queued: "info", scheduled: "info", preparing: "info", sending: "info", open: "info",
  pending: "warn", paused: "warn", draft: "muted", cancelled: "muted", skipped: "muted", expired: "muted", void: "muted", retired: "muted",
  failed: "bad", bounced: "bad", complained: "bad", disabled: "bad", opted_out: "bad", rejected: "bad", revoked: "bad",
};
export const Badge = ({ children }) => {
  const key = String(children).toLowerCase();
  return <span className={`badge ${TONES[key] || "muted"}`}>{t(key)}</span>; // statuset përkthehen këtu
};

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

export const ErrorBox = ({ error, retry }) =>
  error ? (
    <div className="alert bad" role="alert">
      <span>{error.message || String(error)}</span>
      {retry && <button className="link" onClick={retry}>{t("Try again")}</button>}
    </div>
  ) : null;
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
        <thead><tr>{cols.map((c) => <th key={c.key || c.label} className={c.num ? "num" : ""}>{c.label}</th>)}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.id ?? i} onClick={r._onClick} className={r._onClick ? "click" : ""} tabIndex={r._onClick ? 0 : undefined}
              onKeyDown={r._onClick ? (e) => e.key === "Enter" && r._onClick() : undefined}>
              {cols.map((c) => <td key={c.key || c.label} className={c.num ? "num" : ""} data-label={c.label}>{c.render ? c.render(r) : r[c.key]}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
      {footer}
    </div>
  );
}

export function CopyButton({ text, label = t("Copy") }) {
  const { toast } = useUi();
  return <button type="button" className="btn small" onClick={async () => { try { await navigator.clipboard.writeText(text); toast(t("Copied to clipboard")); } catch { toast(t("Couldn't copy, select and copy manually"), "bad"); } }}>{label}</button>;
}

// Vlerë e ndjeshme që shfaqet një herë (çelës API, sekret webhook)
export function SecretBanner({ title, value, note, onClose }) {
  return (
    <div className="secret" role="alert">
      <div><b>{title}</b><div className="muted">{note}</div></div>
      <code>{value}</code>
      <div className="row"><CopyButton text={value} /><Button onClick={onClose}>{t("I've saved it")}</Button></div>
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

// Segmentet e SMS-it (GSM-7 / UCS-2) njësoj si serveri, për numërues live.
const GSM = "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà";
export function smsInfo(text) {
  let len = 0, ucs = false;
  for (const ch of text) { if (GSM.includes(ch)) len += 1; else if ("^{}\\[~]|€".includes(ch)) len += 2; else { ucs = true; break; } }
  if (ucs) len = [...text].reduce((n, c) => n + (c.codePointAt(0) > 0xffff ? 2 : 1), 0);
  const [single, multi] = ucs ? [70, 67] : [160, 153];
  return { encoding: ucs ? "Unicode" : "GSM-7", ucs, length: len, segments: len === 0 ? 0 : len <= single ? 1 : Math.ceil(len / multi), perSegment: len <= single ? single : multi };
}

export function useDebounced(value, ms = 350) {
  const [v, setV] = useState(value);
  useEffect(() => { const timer = setTimeout(() => setV(value), ms); return () => clearTimeout(timer); }, [value, ms]);
  return v;
}
