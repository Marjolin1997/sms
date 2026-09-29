import { useCallback, useEffect, useState } from "react";

export function useLoad(fn, deps = [], refreshMs = 0) {
  const [state, set] = useState({ data: null, error: null, loading: true });
  const load = useCallback(async () => {
    try {
      set((s) => ({ ...s, error: null }));
      const data = await fn();
      set({ data, error: null, loading: false });
    } catch (e) {
      set({ data: null, error: e, loading: false });
    }
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    load();
    if (!refreshMs) return;
    const t = setInterval(load, refreshMs);
    return () => clearInterval(t);
  }, [load, refreshMs]);
  return { ...state, reload: load };
}

export const Card = ({ title, actions, children, className = "" }) => (
  <section className={`card ${className}`}>
    {(title || actions) && (
      <header className="card-h">
        <h3>{title}</h3>
        <div className="row">{actions}</div>
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
  delivered: "good", completed: "good", verified: "good", active: "good", succeeded: "good", ok: "good",
  sent: "info", running: "info", queued: "info", scheduled: "info", preparing: "info", sending: "info", pending: "warn",
  paused: "warn", draft: "muted", cancelled: "muted", skipped: "muted",
  failed: "bad", bounced: "bad", complained: "bad", disabled: "bad", opted_out: "bad",
};
export const Badge = ({ children }) => <span className={`badge ${TONES[String(children).toLowerCase()] || "muted"}`}>{children}</span>;

export const Button = ({ variant = "", busy, ...p }) => <button {...p} disabled={p.disabled || busy} className={`btn ${variant} ${p.className || ""}`} />;

export const Field = ({ label, hint, children }) => (
  <label className="field">
    <span>{label}</span>
    {children}
    {hint && <small>{hint}</small>}
  </label>
);

export const Empty = ({ children }) => <div className="empty">{children}</div>;
export const ErrorBox = ({ error }) => (error ? <div className="alert bad">{error.message || String(error)}</div> : null);

export function Table({ cols, rows, empty = "Nothing here yet." }) {
  if (!rows || rows.length === 0) return <Empty>{empty}</Empty>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>{cols.map((c) => <th key={c.key || c.label} className={c.num ? "num" : ""}>{c.label}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.id ?? i} onClick={r._onClick} className={r._onClick ? "click" : ""}>
              {cols.map((c) => <td key={c.key || c.label} className={c.num ? "num" : ""}>{c.render ? c.render(r) : r[c.key]}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Ekzekuton një veprim me gjendje "duke punuar" dhe njoftim suksesi/gabimi.
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [ok, setOk] = useState("");
  const run = async (fn, okMsg = "Done") => {
    setBusy(true); setError(null); setOk("");
    try { const r = await fn(); setOk(okMsg); return r; }
    catch (e) { setError(e); return undefined; }
    finally { setBusy(false); }
  };
  return { busy, error, ok, run };
}

export const Notice = ({ error, ok }) => (
  <>
    <ErrorBox error={error} />
    {ok && <div className="alert good">{ok}</div>}
  </>
);

export const money = (v) => (v == null ? "-" : Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 }));
export const when = (v) => (v ? new Date(v).toLocaleString() : "-");
