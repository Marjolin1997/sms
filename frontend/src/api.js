// Klient i vogël API. Çelësi ruhet në sessionStorage (fshihet kur mbyllet skeda).
const STORE = "sms_api_key";

export const getKey = () => sessionStorage.getItem(STORE) || "";
export const setKey = (k) => (k ? sessionStorage.setItem(STORE, k) : sessionStorage.removeItem(STORE));

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

let owner = ""; // llogaria që shohin/administrojnë stafi; klienti e ka të fiksuar
export const setOwner = (o) => (owner = o || "");

async function request(method, path, { params = {}, body, headers = {} } = {}) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...(owner ? { owner_ref: owner } : {}), ...params }))
    if (v !== undefined && v !== null && v !== "") qs.set(k, v);
  const url = qs.toString() ? `${path}?${qs}` : path;
  const payload = body && owner && !Array.isArray(body) ? { owner_ref: owner, ...body } : body;
  const res = await fetch(url, {
    method,
    headers: {
      Authorization: `Bearer ${getKey()}`,
      ...(payload ? { "Content-Type": "application/json" } : {}),
      ...headers,
    },
    body: payload ? JSON.stringify(payload) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : d && d.message ? d.message : Array.isArray(d) ? d.map((x) => x.msg).join("; ") : `HTTP ${res.status}`;
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
};

export const uuid = () => crypto.randomUUID();
