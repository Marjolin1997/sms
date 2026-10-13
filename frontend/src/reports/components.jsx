import { useEffect, useState } from "react";
import { T, t } from "../i18n.jsx";
import { Button } from "../ui.jsx";
import { rangeProblem, shiftDay, utcToday } from "./range.js";

const PRESETS = [[7, T("Last 7 days")], [30, T("Last 30 days")], [90, T("Last 90 days")]];
const PROBLEMS = { format: T("Enter both dates."), order: T("The end date must not be before the start date."), long: T("The range is limited to 366 days.") };

/** Filtri i datave: validim lokal (UX) para çdo kërkese; `onChange(from, to)` thirret vetëm me interval të vlefshëm. Ditët janë UTC. */
export function DateRange({ from, to, onChange }) {
  const [f, setF] = useState(from), [e, setE] = useState(to);
  useEffect(() => { setF(from); setE(to); }, [from, to]);
  const problem = rangeProblem(f, e);
  const set = (nf, ne) => { setF(nf); setE(ne); if (!rangeProblem(nf, ne)) onChange(nf, ne); };
  const today = utcToday();
  return (
    <div className="daterange" role="group" aria-label={t("Date range (UTC days)")}>
      <label>{t("From")} <input type="date" value={f} max={e || undefined} onChange={(ev) => set(ev.target.value, e)} aria-invalid={!!problem} /></label>
      <label>{t("To")} <input type="date" value={e} min={f || undefined} onChange={(ev) => set(f, ev.target.value)} aria-invalid={!!problem} /></label>
      {PRESETS.map(([n, label]) => <Button key={n} onClick={() => set(shiftDay(today, -(n - 1)), today)}>{t(label)}</Button>)}
      {problem && <div className="alert warn" role="alert"><span>{t(PROBLEMS[problem])}</span></div>}
    </div>
  );
}

/** Kolonë e segmentuar nga numërues të serverit (delivered / failed / të tjerat). Teksti dhe numrat janë gjithmonë të dukshëm (jo vetëm ngjyrë). */
export function SplitBar({ delivered = 0, failed = 0, other = 0, max = 1 }) {
  const w = (n) => `${(n / Math.max(1, max)) * 100}%`;
  const label = t("{d} delivered, {f} failed, {o} other", { d: delivered, f: failed, o: other });
  return (
    <div className="bar split" role="img" aria-label={label} title={label}>
      <div className="fill ok" style={{ width: w(delivered) }} /><div className="fill bad" style={{ width: w(failed) }} /><div className="fill" style={{ width: w(other) }} />
    </div>
  );
}
