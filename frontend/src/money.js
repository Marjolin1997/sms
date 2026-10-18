// Formatim i saktë i shumave (M11-E): shumat vijnë nga backend-i si string dhjetor dhe NUK kalojnë nëpër Number.
// Vetëm paraqitje: asnjë shumë nuk llogaritet këtu.
import { locale } from "./i18n.jsx";

const AMOUNT = /^(-)?(\d+)(?:\.(\d+))?$/;

/** "1234.500000" → "1,234.50" (ose "1.234,50"): grupim me string, të paktën 2 dhe deri në 6 decimale. Pavlefshme ⇒ "-". */
export function amt(value) {
  if (typeof value !== "string") return "-";
  const m = AMOUNT.exec(value.trim());
  if (!m) return "-";
  const parts = new Intl.NumberFormat(locale()).formatToParts(11111.1);
  const group = parts.find((p) => p.type === "group")?.value ?? ",";
  const decimal = parts.find((p) => p.type === "decimal")?.value ?? ".";
  const frac = (m[3] || "").replace(/0+$/, "");
  return `${m[1] ? "-" : ""}${m[2].replace(/^0+(?=\d)/, "").replace(/\B(?=(\d{3})+(?!\d))/g, group)}${decimal}${frac.length < 2 ? frac.padEnd(2, "0") : frac}`;
}

/** Datë kontabiliteti: vetëm data, në UTC (nuk zhvendoset nga zona e shfletuesit). */
export const accountingDate = (v) => {
  const d = v ? new Date(v) : null;
  return d && !Number.isNaN(d.getTime()) ? d.toLocaleDateString(locale(), { timeZone: "UTC", year: "numeric", month: "2-digit", day: "2-digit" }) : "-";
};

/** Sasia e rreshtit të faturës (string i backend-it): grupim i mijëshave, pa zero të tepërta. */
export function qty(value) {
  if (typeof value !== "string") return "-";
  const m = AMOUNT.exec(value.trim());
  if (!m) return "-";
  const decimal = new Intl.NumberFormat(locale()).formatToParts(1.1).find((p) => p.type === "decimal")?.value ?? ".";
  const group = new Intl.NumberFormat(locale()).formatToParts(11111).find((p) => p.type === "group")?.value ?? ",";
  return `${m[1] ? "-" : ""}${m[2].replace(/\B(?=(\d{3})+(?!\d))/g, group)}${m[3] && /[1-9]/.test(m[3]) ? decimal + m[3].replace(/0+$/, "") : ""}`;
}
