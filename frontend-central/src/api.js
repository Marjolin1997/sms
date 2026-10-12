import { T, t } from "./i18n.jsx";

// Klient API për CENTRAL (admin/operator). Auth i veçantë nga Enterprise: JWT nga POST /auth/token.
// Token-i ruhet në sessionStorage (fshihet me mbylljen e skedës), kurrë në URL, log ose DOM. Fjalëkalimi nuk ruhet.
const STORE = "central_token";
export const getToken = () => { try { return sessionStorage.getItem(STORE) || ""; } catch { return ""; } };
export const setToken = (v) => { try { v ? sessionStorage.setItem(STORE, v) : sessionStorage.removeItem(STORE); } catch { /* storage i bllokuar */ } };

// Mesazhe sipas kodit. `forbidden` = roli (operator) s'ka të drejtë shkrimi; 401 trajtohet nga handler-i i skadimit.
const FRIENDLY = {
  forbidden: T("Your role is read-only for this action."),
  invalid_credentials: T("That email or password isn't right."),
  unauthorized: T("Your session has ended. Please sign in again."),
  auth_not_configured: T("Sign-in is unavailable right now."),
  not_found: T("We couldn't find that. It may have been removed."),
};

// Mesazhe domain të Central (anglisht, të kuruara): përkthehen me shabllon; të tjerat shfaqen siç janë (409/422 janë mesazhe për staf).
const SERVER_MESSAGES = [
  [/^sender id already approved for another account$/, T("This sender identifier is already approved for another account.")],
  [/^policy denies (\w+) senders in (\w+) \(revision (\w+)\): approval is not possible$/, T("Policy denies {kind} senders in {country} (revision {rev}), so approval isn't possible."), ["kind", "country", "rev"]],
  [/^cannot (\w+) from status (\w+)$/, T("This action isn't possible while the sender is {status}. The details were refreshed."), [null, "status"]],
  [/^a newer policy revision already took effect at this instant; retry$/, T("Another change was saved at the same moment. Try again.")],
  [/^a concurrent policy revision was created for this country and sender kind$/, T("Another change was saved at the same moment. Try again.")],
  [/^a country\/kind that is not allowed must keep requires_approval=true/, T("A country that isn't allowed must keep “Requires approval” on.")],
  [/^sender not found$/, T("Sender not found.")],
  [/^sender policy not found$/, T("Policy not found.")],
  [/^a request for this sender is already in progress$/, T("A request for this sender is already in progress.")],
];

function serverMessage(msg) {
  for (const [re, key, names] of SERVER_MESSAGES) {
    const m = String(msg).match(re);
    if (m) return t(key, names ? Object.fromEntries(names.map((n, i) => (n ? [n, m[i + 1]] : null)).filter(Boolean)) : undefined);
  }
  return msg;
}

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
    this.friendly = !!FRIENDLY[code] || ["conflict", "invalid", "not_found"].includes(code);
    this.fields = extra.fields || {};
    this.retryAfter = extra.retryAfter ?? null;
  }
  get retryable() { return ["network", "server", "rate_limited"].includes(this.kind); }
}

// Pydantic: detail=[{loc:["body","reason"], msg}] → { reason: msg }. Gabimet domain (code "invalid") lidhen me fushën nga emri i saj te mesazhi.
function fieldErrors(detail) {
  const out = {};
  if (Array.isArray(detail)) {
    for (const x of detail) {
      const key = (x.loc || []).slice(1).join(".");
      if (key && !out[key]) out[key] = x.msg;
    }
  } else if (detail && detail.code === "invalid" && typeof detail.message === "string") {
    for (const f of ["requires_approval", "evidence_ref", "reason", "country", "sender_kind", "issued_from", "issued_to"]) {
      if (detail.message.includes(f)) { out[f] = serverMessage(detail.message); break; }
    }
  }
  return out;
}

let onSessionExpired = null;
export const setSessionExpiredHandler = (fn) => (onSessionExpired = fn);
export const REQUEST_TIMEOUT_MS = 30000;

async function request(method, path, { params = {}, body, signal, timeoutMs = REQUEST_TIMEOUT_MS, auth = true, blob = false } = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") qs.set(k, v);
  const url = qs.toString() ? `${path}?${qs}` : path;
  const ctl = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; ctl.abort(); }, timeoutMs);
  const onAbort = () => ctl.abort();
  if (signal) { if (signal.aborted) ctl.abort(); else signal.addEventListener("abort", onAbort, { once: true }); }
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: { ...(auth && getToken() ? { Authorization: `Bearer ${getToken()}` } : {}), ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: ctl.signal,
    });
  } catch (e) {
    if (!timedOut && signal?.aborted) throw e;
    throw new ApiError(0, timedOut ? "timeout" : "network", timedOut ? t("The server took too long to answer. Try again.") : t("Can't reach the server. Check your connection and try again."));
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", onAbort);
  }
  if (blob && res.ok) return res; // trupi binar (PDF): thirrësi e lexon si blob; gabimet mbeten JSON dhe kalojnë më poshtë
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : d && d.message ? d.message : Array.isArray(d) ? d.map((x) => `${(x.loc || []).slice(1).join(".")}: ${x.msg}`).join("; ") : t("Request failed (HTTP {status})", { status: res.status });
    const ra = Number(res.headers?.get?.("Retry-After"));
    const err = new ApiError(res.status, d && d.code, msg, { fields: fieldErrors(d), retryAfter: Number.isFinite(ra) && ra > 0 ? ra : null });
    if (res.status === 401 && auth && onSessionExpired && getToken()) onSessionExpired(err); // 403 NUK e mbyll sesionin
    throw err;
  }
  return data;
}

// Ruan një blob si skedar (anchor me object URL, i revokuar pas pak). Emri vjen nga serveri dhe lejohet vetëm [A-Za-z0-9._-].
export function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.rel = "noopener"; a.style.display = "none";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

export function normalizePage(data, limit = 50, offset = 0) {
  return { items: (data && data.items) || [], hasMore: !!(data && data.has_more), limit: data?.limit ?? limit, offset: data?.offset ?? offset };
}

export const api = {
  get: (p, params, opts = {}) => request("GET", p, { params, ...opts }),
  post: (p, body, opts = {}) => request("POST", p, { body, ...opts }),
  /** PDF zyrtar nga serveri → skedar. Vetëm application/pdf jo bosh pranohet; 401/403/404/5xx kalojnë si ApiError. */
  downloadPdf: async (p, fallbackName = "invoice.pdf", opts = {}) => {
    const res = await request("GET", p, { blob: true, ...opts });
    const blob = await res.blob();
    if (!/^application\/pdf/i.test(res.headers?.get?.("Content-Type") || "") || blob.size === 0) throw new ApiError(502, "bad_document", t("The server returned an unexpected document."));
    const m = /filename="([A-Za-z0-9._-]{1,80})"/.exec(res.headers.get("Content-Disposition") || "");
    saveBlob(blob, m ? m[1] : fallbackName);
  },
  page: async (p, { limit = 50, offset = 0, ...filters } = {}, opts = {}) => normalizePage(await request("GET", p, { params: { limit, offset, ...filters }, ...opts }), limit, offset),
};

/** Hyrja: POST /auth/token → ruan token-in dhe kthen profilin nga /auth/me. Fjalëkalimi nuk ruhet askund. */
export async function login(email, password) {
  const r = await request("POST", "/auth/token", { body: { email: email.trim(), password }, auth: false });
  setToken(r.access_token);
  try { return await api.get("/auth/me"); } catch (e) { setToken(""); throw e; }
}
