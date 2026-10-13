import { useEffect, useState } from "react";
import { api } from "../api.js";
import { T, t } from "../i18n.jsx";
import { Button, ErrorBox, FilterBar, useAction } from "../ui.jsx";
import { buildHash } from "../routes.js";

// Ditët janë UTC; të dyja skajet përfshihen (serveri është autoritet; këtu vetëm UX).
export const isDay = (s) => /^\d{4}-\d{2}-\d{2}$/.test(s || "") && !Number.isNaN(Date.parse(`${s}T00:00:00Z`));
export const rangeProblem = (from, to) => (from && !isDay(from)) || (to && !isDay(to)) ? "format" : from && to && from > to ? "order" : from && to && (Date.parse(to) - Date.parse(from)) / 86400000 + 1 > 366 ? "long" : null;
const PROBLEMS = { format: T("Enter a valid date."), order: T("The end date must not be before the start date."), long: T("The range is limited to 366 days.") };

export const goTo = (routeId, query) => { location.hash = buildHash(routeId, [], query); };

/** Filtri i datave për audit: cakton ?from=&to= vetëm kur intervali është i vlefshëm. */
export function DateFilter({ from, to, onChange }) {
  const [f, setF] = useState(from || ""), [e, setE] = useState(to || "");
  useEffect(() => { setF(from || ""); setE(to || ""); }, [from, to]);
  const problem = rangeProblem(f, e);
  const set = (nf, ne) => { setF(nf); setE(ne); if (!rangeProblem(nf, ne)) onChange(nf, ne); };
  return (
    <>
      <label className="inline">{t("From")} <input type="date" value={f} onChange={(ev) => set(ev.target.value, e)} aria-invalid={!!problem} /></label>
      <label className="inline">{t("To")} <input type="date" value={e} onChange={(ev) => set(f, ev.target.value)} aria-invalid={!!problem} /></label>
      {problem && <div className="alert warn" role="alert"><span>{t(PROBLEMS[problem])}</span></div>}
    </>
  );
}

/** Butoni i eksportit CSV: shkarkim nga serveri (i kufizuar) me të njëjtat filtra; gabimi (p.sh. shumë rreshta) shfaqet në vend. */
export function ExportButton({ path, params, file }) {
  const a = useAction();
  return (
    <>
      <Button busy={a.busy} onClick={() => a.run(() => api.downloadCsv(path, params, file), t("Download started"))}>{t("Export CSV")}</Button>
      <ErrorBox error={a.error} />
    </>
  );
}

export const Filters = ({ label, children }) => <FilterBar label={label}>{children}</FilterBar>;
