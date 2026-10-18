// Formatim i parave dhe datave të kontabilitetit. VETËM paraqitje: asnjë shumë nuk llogaritet këtu.
// Shumat vijnë nga backend-i si string dhjetor ("1234.500000"); NUK kalojnë kurrë nëpër Number (humbet saktësinë).
import { locale } from "../i18n.jsx";

const AMOUNT = /^(-)?(\d+)(?:\.(\d+))?$/;

function separators() {
  const parts = new Intl.NumberFormat(locale()).formatToParts(11111.1);
  return { group: parts.find((p) => p.type === "group")?.value ?? ",", decimal: parts.find((p) => p.type === "decimal")?.value ?? "." };
}

/** "1234.500000" → "1,234.50" (ose "1.234,50"): grupim i mijëshave me string, të paktën 2 decimale, deri në 6 (zerot e tepërta hiqen). Pavlefshme ⇒ "-". */
export function formatAmount(value) {
  if (typeof value !== "string") return "-";
  const m = AMOUNT.exec(value.trim());
  if (!m) return "-";
  const [, neg, int, frac = ""] = m;
  const { group, decimal } = separators();
  const trimmed = frac.replace(/0+$/, "");
  const decimals = trimmed.length < 2 ? trimmed.padEnd(2, "0") : trimmed;
  const grouped = int.replace(/^0+(?=\d)/, "").replace(/\B(?=(\d{3})+(?!\d))/g, group);
  return `${neg ? "-" : ""}${grouped}${decimal}${decimals}`;
}

/** Shuma + monedha si tekst. Monedha pa vlerë ⇒ vetëm shuma. */
export const formatMoney = (value, currency) => {
  const a = formatAmount(value);
  return a === "-" || !currency ? a : `${a} ${currency}`;
};

/** Sasi e faturës ("1500", "2.5"): string i backend-it, grupim i mijëshave, pa zero të tepërta. */
export function formatQuantity(value) {
  if (typeof value !== "string") return "-";
  const m = AMOUNT.exec(value.trim());
  if (!m) return "-";
  const { group, decimal } = separators();
  return `${m[1] ? "-" : ""}${m[2].replace(/\B(?=(\d{3})+(?!\d))/g, group)}${m[3] && /[1-9]/.test(m[3]) ? decimal + m[3].replace(/0+$/, "") : ""}`;
}

const parseIso = (v) => { const d = v ? new Date(v) : null; return d && !Number.isNaN(d.getTime()) ? d : null; };

/** Datë kontabiliteti: vetëm data, gjithmonë në UTC (nuk zhvendoset nga zona e shfletuesit). */
export function accountingDate(v) {
  const d = parseIso(v);
  return d ? d.toLocaleDateString(locale(), { timeZone: "UTC", year: "numeric", month: "2-digit", day: "2-digit" }) : "-";
}

/** Moment i saktë në UTC. */
export function exactUtc(v) {
  const d = parseIso(v);
  return d ? `${d.toLocaleString(locale(), { timeZone: "UTC", hour12: false, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })} UTC` : "-";
}

/** Statusi i serverit PASS|WARN|FAIL|CRITICAL → pass|warn|fail|critical; çdo gjë tjetër ose e munguar ⇒ "unknown" (kurrë "pass"). */
export function level(s) {
  const v = typeof s === "string" ? s.toLowerCase() : "";
  return ["pass", "warn", "fail", "critical"].includes(v) ? v : "unknown";
}
