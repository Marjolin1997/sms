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

export class ApiError extends Error {
  constructor(status, code, message) {
    super(FRIENDLY[code] ? t(FRIENDLY[code]) : serverMessage(message));
    this.status = status;
    this.code = code;
    this.raw = message;
  }
}

let owner = ""; // llogaria që shohin/administrojnë stafi; klienti e ka të fiksuar
export const setOwner = (o) => (owner = o || "");
export const currentOwner = () => owner;

// Hapi i dytë (TOTP): UiProvider regjistron një funksion që kërkon kodin nga përdoruesi.
let totpPrompt = null;
export const setTotpPrompt = (fn) => (totpPrompt = fn);

async function request(method, path, { params = {}, body, headers = {}, attempt = 0 } = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...(owner ? { owner_ref: owner } : {}), ...params }))
    if (v !== undefined && v !== null && v !== "") qs.set(k, v);
  const url = qs.toString() ? `${path}?${qs}` : path;
  const payload = body && owner && !Array.isArray(body) ? { owner_ref: owner, ...body } : body;
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: { Authorization: `Bearer ${getKey()}`, ...(payload ? { "Content-Type": "application/json" } : {}), ...headers },
      body: payload ? JSON.stringify(payload) : undefined,
    });
  } catch {
    throw new ApiError(0, "network", t("Can't reach the server. Check your connection and try again."));
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : d && d.message ? d.message : Array.isArray(d) ? d.map((x) => `${(x.loc || []).slice(1).join(".")}: ${x.msg}`).join("; ") : t("Request failed (HTTP {status})", { status: res.status });
    const code = d && d.code;
    if ((code === "totp_required" || code === "totp_invalid") && totpPrompt && attempt < 3) {
      const otp = await totpPrompt(code === "totp_invalid");
      if (otp) return request(method, path, { params, body, headers: { ...headers, "X-TOTP": otp }, attempt: attempt + 1 });
    }
    throw new ApiError(res.status, code, msg);
  }
  return data;
}

export const api = {
  get: (p, params) => request("GET", p, { params }),
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

export const uuid = () => crypto.randomUUID();
