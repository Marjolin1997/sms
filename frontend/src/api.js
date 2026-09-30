// Klient i vogël API. Çelësi ruhet në sessionStorage (fshihet kur mbyllet skeda).
const STORE = "sms_api_key";

// "Më mbaj të hyrë" ruan tokenin në localStorage; përndryshe vetëm për këtë skedë.
export const getKey = () => sessionStorage.getItem(STORE) || localStorage.getItem(STORE) || "";
export function setKey(k, remember = false) {
  sessionStorage.removeItem(STORE);
  localStorage.removeItem(STORE);
  if (k) (remember ? localStorage : sessionStorage).setItem(STORE, k);
}

// Mesazhe në gjuhë të thjeshtë për kodet e gabimit që i shohin përdoruesit.
const FRIENDLY = {
  insufficient_funds: "Your wallet balance is too low for this. Top up on the Wallet page and try again.",
  sender_not_allowed: "That sender ID isn't approved for the destination country yet. Request it on the Sender IDs page.",
  recipient_suppressed: "This person can't be contacted: they opted out, or there is no recorded consent for marketing messages.",
  no_route: "We can't deliver to that country yet. Contact support if you need it enabled.",
  no_rate: "There is no price for that destination yet. Contact support.",
  invalid_number: "Enter the number in international format, for example +355691234567.",
  account_disabled: "Your account can't send yet. Contact support to activate sending.",
  rate_limited: "You're sending too fast. Wait a moment and try again.",
  sending_paused: "Sending is temporarily paused by the platform. Please try again shortly.",
  template_not_usable: "That template has no approved version yet.",
  sender_domain_not_verified: "The from address must be on a domain you have verified under Email domains.",
  unauthorized: "Your session is no longer valid. Please sign in again.",
  session_required: "This needs a personal sign-in with email and password, not an API key.",
  forbidden: "Your role doesn't allow this action.",
};

export class ApiError extends Error {
  constructor(status, code, message) {
    super(FRIENDLY[code] || message);
    this.status = status;
    this.code = code;
    this.raw = message;
  }
}

let owner = ""; // llogaria që shohin/administrojnë stafi; klienti e ka të fiksuar
export const setOwner = (o) => (owner = o || "");
export const currentOwner = () => owner;

async function request(method, path, { params = {}, body, headers = {} } = {}) {
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
    throw new ApiError(0, "network", "Can't reach the server. Check your connection and try again.");
  }
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (res.status === 401 && getKey() && !path.startsWith("/v1/auth/")) window.dispatchEvent(new Event("sms:unauthorized"));
  if (!res.ok) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : d && d.message ? d.message : Array.isArray(d) ? d.map((x) => `${(x.loc || []).slice(1).join(".")}: ${x.msg}`).join("; ") : `Request failed (HTTP ${res.status})`;
    throw new ApiError(res.status, d && d.code, msg);
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
  if (!res.ok) throw new ApiError(res.status, "error", `Couldn't open the document (HTTP ${res.status})`);
  const url = URL.createObjectURL(new Blob([await res.text()], { type: "text/html" }));
  window.open(url, "_blank", "noopener");
}

export const uuid = () => crypto.randomUUID();

// CSV i mbrojtur: merret me Authorization dhe shkarkohet si skedar.
export async function downloadFile(path, params = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...(owner ? { owner_ref: owner } : {}), ...params })) if (v) qs.set(k, v);
  let res;
  try { res = await fetch(`${path}?${qs}`, { headers: { Authorization: `Bearer ${getKey()}` } }); }
  catch { throw new ApiError(0, "network", "Can't reach the server. Check your connection and try again."); }
  if (!res.ok) throw new ApiError(res.status, "error", `Couldn't download the file (HTTP ${res.status})`);
  const name = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") || "")?.[1] || "export.csv";
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
