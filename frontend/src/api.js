import { T, t } from "./i18n.jsx";

// Klient i vogël API. Çelësi ruhet në sessionStorage (fshihet kur mbyllet skeda).
const STORE = "sms_api_key";

export const getKey = () => sessionStorage.getItem(STORE) || "";
export const setKey = (k) => (k ? sessionStorage.setItem(STORE, k) : sessionStorage.removeItem(STORE));

// Mesazhe në gjuhë të thjeshtë për kodet e gabimit që i shohin përdoruesit.
const FRIENDLY = {
  insufficient_funds: T("Your wallet balance is too low for this. Top up on the Wallet page and try again."),
  sender_not_allowed: T("That sender ID isn't approved for the destination country yet. Request it on the Sender IDs page."),
  recipient_suppressed: T("This person can't be contacted: they opted out, or there is no recorded consent for marketing messages."),
  no_route: T("We can't deliver to that country yet. Contact support if you need it enabled."),
  no_rate: T("There is no price for that destination yet. Contact support."),
  invalid_number: T("Enter the number in international format, for example +355691234567."),
  account_disabled: T("Your account can't send yet. Contact support to activate sending."),
  rate_limited: T("You're sending too fast. Wait a moment and try again."),
  sending_paused: T("Sending is temporarily paused by the platform. Please try again shortly."),
  template_not_usable: T("That template has no approved version yet."),
  sender_domain_not_verified: T("The from address must be on a domain you have verified under Email domains."),
  unauthorized: T("Your session is no longer valid. Please sign in again."),
  totp_required: T("This action needs your two-factor code."),
  totp_invalid: T("That two-factor code isn't right or was already used. Wait for the next code and try again."),
  totp_enrollment_required: T("Turn on two-factor authentication first (Admin → Security)."),
  no_key: T("Two-factor works with API keys, not with the bootstrap key."),
  payments_disabled: T("Online payments aren't available yet. Contact us to top up your wallet."),
  too_many_attempts: T("Too many failed sign-in attempts from this network. Wait a few minutes and try again."),
  ip_not_allowed: T("This key isn't allowed from your current network address."),
  forbidden: T("Your role doesn't allow this action."),
  not_found: T("We couldn't find that. It may have been removed."),
  gateway_error: T("The payment service is unavailable right now. Please try again in a moment."),
  invalid_domain: T("That doesn't look like a valid domain name."),
  invalid_email: T("Check the email details and try again."),
  invalid_contact: T("Check the contact details and try again."),
  invalid_address: T("That address isn't valid."),
  invalid_webhook: T("That webhook address or event selection isn't allowed."),
  invalid_template: T("The template wording isn't valid. Blanks are written like {{name}}."),
  invalid_sender: T("That sender ID isn't valid. Use 3-11 letters or digits (with a letter), or a phone number."),
  invalid_campaign: T("Check the campaign details and try again."),
  invalid_billing: T("Check the billing details and try again."),
  invalid_key: T("Check the key details and try again."),
  invalid_message: T("Check the message and try again."),
  conflict: T("This conflicts with what already exists. Refresh and try again."),
  // Sender flow (M10): kodet e domain-it shfaqen si tekst i qartë, kurrë detaje të brendshme.
  sender_authority_frozen: T("Sender approvals are managed centrally now, so this can't be changed here."),
  sender_policy_denied: T("Sending with this sender ID is not allowed for that country."),
  sender_revoked: T("This sender ID was revoked. Submit it again for review."),
  sender_rejected: T("This sender ID was rejected. Fix it and submit again."),
  sender_pending: T("This sender ID is still waiting for approval."),
  not_synchronized: T("This sender ID is not synchronized yet. Try again shortly."),
  validation_error: T("Check the highlighted details and try again."),
};

// Mesazhe të serverit (anglisht) që shfaqen shpesh: përkthehen me shabllon. Të tjerat shfaqen siç janë.
const SERVER_MESSAGES = [
  [/^template name already exists$/, T("You already have a template with that name. Choose another name.")],
  [/^campaign name already exists$/, T("You already have a campaign with that name. Choose another name.")],
  [/^list name already exists$/, T("You already have a list with that name.")],
  [/^domain already added$/, T("You've already added that domain.")],
  [/^wallet not found$/, T("Wallet not found.")],
  [/^creator cannot confirm own top-up$/, T("Someone else must confirm this top-up (separation of duties).")],
  [/^external_ref already used for a different top-up$/, T("That reference was already used for a different top-up.")],
  [/^amount must be between (.+) and (.+)$/, T("The amount must be between {a} and {b}."), ["a", "b"]],
  [/^amount has more than 6 decimal places/, T("Use at most 6 decimals for an amount.")],
  [/^amount must be > 0$/, T("The amount must be greater than zero.")],
  [/^invoice is (\w+)$/, T("This invoice is {status}, so it can't be changed."), ["status"]],
  [/^only open invoices can be voided/, T("Only unpaid invoices can be voided. A paid invoice needs a credit note.")],
  [/^a billing profile is required before subscribing$/, T("The customer must fill in billing details first.")],
  [/^the customer has not filled in billing details yet$/, T("The customer hasn't filled in billing details yet.")],
  [/^at most (\d+) webhook endpoints per account$/, T("You can have at most {n} webhook endpoints."), ["n"]],
  [/^at most (\d+) active keys per account$/, T("You can have at most {n} active keys."), ["n"]],
  [/^cannot schedule from status (\w+)$/, T("A campaign that is {status} can't be started."), ["status"]],
  [/^sender id is not approved for this account$/, T("That sender ID isn't approved yet.")],
  [/^from_email is not on a verified domain/, T("The from address isn't on a verified domain.")],
  [/^effective_from must not be in the past$/, T("The start time must be in the future.")],
  [/^effective_from must be after the previous published version$/, T("The start time must be after the previous version's start.")],
  [/^cannot publish an empty version$/, T("Add at least one price before publishing.")],
  [/^a draft already exists$/, T("There is already a draft version. Finish or publish it first.")],
  [/^version is published and immutable$/, T("Published prices can't be edited. Create a new draft version.")],
  [/^a reason is required$/, T("A reason is required.")],
  [/^opt-in requires evidence/, T("Write down how and when the person agreed.")],
  [/^address is blocked \((\w+)\) and cannot be re-subscribed$/, T("This address is blocked ({reason}) and can't be re-subscribed."), ["reason"]],
  [/^insufficient available funds$/, T("Not enough balance.")],
  [/^client keys must be bound to an owner_ref$/, T("Choose an account for this customer key.")],
];

function serverMessage(msg) {
  for (const [re, key, names] of SERVER_MESSAGES) {
    const m = msg.match(re);
    if (m) return t(key, names ? Object.fromEntries(names.map((n, i) => [n, t(m[i + 1])])) : undefined);
  }
  return msg;
}

// Klasifikim i vetëm i gabimeve për UI-n (kind): faqet nuk krahasojnë statuse HTTP vetë.
export function errorKind(status) {
  if (status === 0) return "network";
  if (status === 401) return "unauthenticated";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 429) return "rate_limited";
  if (status === 400 || status === 422) return "validation";
  if (status >= 500) return "server";
  return "other";
}

export class ApiError extends Error {
  constructor(status, code, message, extra = {}) {
    super(FRIENDLY[code] ? t(FRIENDLY[code]) : serverMessage(message));
    this.status = status;
    this.code = code;
    this.raw = message;
    this.kind = errorKind(status);
    this.friendly = !!FRIENDLY[code]; // kodi i njohur ⇒ mesazhi është i sigurt për t'u shfaqur
    this.fields = extra.fields || {}; // gabime për fushë: { "emri": "mesazhi" } (serveri mbetet autoritet)
    this.retryAfter = extra.retryAfter ?? null; // sekonda (429/503) kur serveri e dërgon
  }
  get retryable() { return ["network", "server", "rate_limited"].includes(this.kind); }
}

// Fushat e validimit FastAPI/Pydantic: detail = [{loc:["body","emri"], msg}] → { emri: msg }.
function fieldErrors(detail) {
  if (!Array.isArray(detail)) return {};
  const out = {};
  for (const x of detail) {
    const key = (x.loc || []).slice(1).join(".");
    if (key && !out[key]) out[key] = x.msg;
  }
  return out;
}

// Sesioni skadoi (401 gjatë një sesioni aktiv): App regjistron një callback që e çon përdoruesin te hyrja.
let onSessionExpired = null;
export const setSessionExpiredHandler = (fn) => (onSessionExpired = fn);

export const REQUEST_TIMEOUT_MS = 30000;

let owner = ""; // llogaria që shohin/administrojnë stafi; klienti e ka të fiksuar
export const setOwner = (o) => (owner = o || "");
export const currentOwner = () => owner;

// Hapi i dytë (TOTP): UiProvider regjistron një funksion që kërkon kodin nga përdoruesi.
let totpPrompt = null;
export const setTotpPrompt = (fn) => (totpPrompt = fn);

// noOwner: endpoint-e me skemë strikte (extra=forbid) që NUK pranojnë owner_ref (p.sh. vendimet mbi sender) — mos e injekto.
async function request(method, path, { params = {}, body, headers = {}, attempt = 0, signal, timeoutMs = REQUEST_TIMEOUT_MS, noOwner = false } = {}) {
  const scope = owner && !noOwner ? owner : "";
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...(scope ? { owner_ref: scope } : {}), ...params }))
    if (v !== undefined && v !== null && v !== "") qs.set(k, v);
  const url = qs.toString() ? `${path}?${qs}` : path;
  const payload = body && scope && !Array.isArray(body) ? { owner_ref: scope, ...body } : body;
  const ctl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; ctl.abort(); }, timeoutMs);
  const onAbort = () => ctl.abort();
  if (signal) { if (signal.aborted) ctl.abort(); else signal.addEventListener("abort", onAbort, { once: true }); }
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: { Authorization: `Bearer ${getKey()}`, ...(payload ? { "Content-Type": "application/json" } : {}), ...headers },
      body: payload ? JSON.stringify(payload) : undefined,
      signal: ctl.signal,
    });
  } catch (e) {
    if (!timedOut && signal?.aborted) throw e; // anuluar nga thirrësi: jo gabim për përdoruesin
    throw new ApiError(0, timedOut ? "timeout" : "network", timedOut ? t("The server took too long to answer. Try again.") : t("Can't reach the server. Check your connection and try again."));
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : d && d.message ? d.message : Array.isArray(d) ? d.map((x) => `${(x.loc || []).slice(1).join(".")}: ${x.msg}`).join("; ") : t("Request failed (HTTP {status})", { status: res.status });
    const code = d && d.code;
    const ra = Number(res.headers?.get?.("Retry-After"));
    if ((code === "totp_required" || code === "totp_invalid") && totpPrompt && attempt < 3) {
      const otp = await totpPrompt(code === "totp_invalid");
      if (otp) return request(method, path, { params, body, headers: { ...headers, "X-TOTP": otp }, attempt: attempt + 1, signal, timeoutMs, noOwner });
    }
    const err = new ApiError(res.status, code, msg, { fields: fieldErrors(d), retryAfter: Number.isFinite(ra) && ra > 0 ? ra : null });
    if (res.status === 401 && onSessionExpired && getKey()) onSessionExpired(err); // 403 NUK e mbyll sesionin: s'ka leje
    throw err;
  }
  return data;
}

// Shkarkon një përgjigje (p.sh. CSV) si skedar, me të njëjtin autentikim dhe llogari.
async function download(path, params = {}, filename = "download") {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...(owner ? { owner_ref: owner } : {}), ...params }))
    if (v !== undefined && v !== null && v !== "") qs.set(k, v);
  let res;
  try { res = await fetch(`${path}?${qs}`, { headers: { Authorization: `Bearer ${getKey()}` } }); }
  catch { throw new ApiError(0, "network", t("Can't reach the server. Check your connection and try again.")); }
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    const d = data && data.detail;
    throw new ApiError(res.status, d && d.code, (d && d.message) || t("Request failed (HTTP {status})", { status: res.status }));
  }
  const url = URL.createObjectURL(await res.blob());
  const el = document.createElement("a");
  el.href = url; el.download = filename; el.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function normalizePage(data, limit = 50, offset = 0) {
  if (Array.isArray(data)) return { items: data, hasMore: data.length >= limit && limit > 0, limit, offset };
  const items = (data && data.items) || [];
  return { items, hasMore: !!(data && (data.has_more ?? data.next_before_id != null)), limit: data?.limit ?? limit, offset: data?.offset ?? offset };
}

export const api = {
  download,
  get: (p, params, opts = {}) => request("GET", p, { params, ...opts }), // opts: { signal, timeoutMs }
  // Lista me faqosje: kthen gjithmonë { items, hasMore, limit, offset } (kontratë Central `has_more` ose varg Enterprise).
  page: async (p, { limit = 50, offset = 0, ...filters } = {}, opts = {}) =>
    normalizePage(await request("GET", p, { params: { limit, offset, ...filters }, ...opts }), limit, offset),
  post: (p, body, opts = {}) => request("POST", p, { body, ...opts }),
  put: (p, body) => request("PUT", p, { body }),
  patch: (p, body) => request("PATCH", p, { body }),
  del: (p) => request("DELETE", p),
  owner: () => owner,
};

// Faqe HTML e mbrojtur (faturë): merret me Authorization dhe hapet si blob.
export async function openHtml(path) {
  const res = await fetch(owner ? `${path}?owner_ref=${encodeURIComponent(owner)}` : path, { headers: { Authorization: `Bearer ${getKey()}` } });
  if (!res.ok) throw new ApiError(res.status, "error", t("Couldn't open the document (HTTP {status})", { status: res.status }));
  const url = URL.createObjectURL(new Blob([await res.text()], { type: "text/html" }));
  window.open(url, "_blank", "noopener");
}

// PDF zyrtar (M11-E2): gjenerohet nga serveri nga fatura e ruajtur; merret me Authorization dhe ruhet si skedar (jo nga DOM-i).
// Gabimet kalojnë te ApiError (401/403/404/5xx) pa tekst të papërpunuar të serverit; përgjigjja e pjesshme/jo-PDF refuzohet.
export function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.rel = "noopener"; a.style.display = "none";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

export async function downloadPdf(path, fallbackName = "invoice.pdf") {
  let res;
  try {
    res = await fetch(owner ? `${path}?owner_ref=${encodeURIComponent(owner)}` : path, { headers: { Authorization: `Bearer ${getKey()}` } });
  } catch {
    throw new ApiError(0, "network", t("Can't reach the server. Check your connection and try again."));
  }
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    const code = data && data.detail && data.detail.code;
    throw new ApiError(res.status, code === "pdf_render_failed" ? "pdf_render_failed" : "error", t("Couldn't download the document (HTTP {status})", { status: res.status }));
  }
  const blob = await res.blob();
  if (!/^application\/pdf/i.test(res.headers.get("Content-Type") || "") || blob.size === 0) throw new ApiError(502, "error", t("The server returned an unexpected document."));
  const m = /filename="([A-Za-z0-9._-]{1,80})"/.exec(res.headers.get("Content-Disposition") || "");
  saveBlob(blob, m ? m[1] : fallbackName);
}

export const uuid = () => crypto.randomUUID();
