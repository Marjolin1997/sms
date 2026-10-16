import { T, locale, t } from "../i18n.jsx";

// ---- Orari: zona është gjithmonë e shprehur; instanti i ruajtur është UTC ----
export const browserZone = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "local"; } catch { return "local"; } };

/** "2026-10-13" + "09:30" në zonën e zgjedhur ("utc" | "local") → Date (instant) ose null. Nuk zhvendos në heshtje. */
export function instantFrom(date, time, zone) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date || "") || !/^\d{2}:\d{2}$/.test(time || "")) return null;
  const d = zone === "utc" ? new Date(`${date}T${time}:00Z`) : new Date(`${date}T${time}:00`);
  return Number.isNaN(d.getTime()) ? null : d;
}

export const utcText = (d) => `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
export const localText = (d) => `${d.toLocaleString(locale(), { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false })} (${browserZone()})`;
export const utcOf = (iso) => (iso ? utcText(new Date(iso)) : "-");

// ---- Kategoritë e dështimit (vetëm paraqitje e `error_code` të serverit; kodi i panjohur mbetet i dukshëm) ----
const CATEGORIES = {
  validation: T("Invalid recipient or message"), sender: T("Sender not allowed"), balance: T("Not enough balance"), route: T("No route or price"),
  recipient: T("Recipient can't be contacted"), provider: T("Rejected by the provider"), delivery: T("Not delivered"), other: T("Failed"),
};
const BY_CODE = {
  insufficient_funds: "balance", sender_not_allowed: "sender", sender_revoked: "sender", sender_rejected: "sender", sender_pending: "sender", sender_policy_denied: "sender",
  not_synchronized: "sender", no_route: "route", no_rate: "route", recipient_suppressed: "recipient", invalid_number: "validation", invalid_message: "validation",
};
export function failureInfo(code) {
  if (!code) return null;
  const c = String(code).toLowerCase();
  let cat = BY_CODE[c];
  if (!cat) cat = /^(twilio|provider|carrier|smpp)/.test(c) ? "provider" : /(undeliver|expired|rejected|unreachable)/.test(c) ? "delivery" : "other";
  return { category: cat, label: t(CATEGORIES[cat]), code };
}
export const CAMPAIGN_STATUSES = ["draft", "scheduled", "preparing", "running", "paused", "completed", "cancelled"];
