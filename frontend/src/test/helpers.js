import { vi } from "vitest";

/** fetch i rremë: routes = { "GET /v1/me": data | (req) => data | Response }. Regjistron thirrjet. */
export function mockFetch(routes) {
  const calls = [];
  const fn = vi.fn(async (url, init = {}) => {
    const method = (init.method || "GET").toUpperCase();
    const path = String(url).split("?")[0];
    const key = `${method} ${path}`;
    calls.push({ method, path, url: String(url), headers: init.headers || {}, body: init.body ? JSON.parse(init.body) : undefined });
    const h = routes[key];
    if (h === undefined) return new Response(JSON.stringify({ detail: { code: "not_found", message: `no mock for ${key}` } }), { status: 404 });
    const out = typeof h === "function" ? await h(calls[calls.length - 1]) : h;
    if (out instanceof Response) return out;
    return new Response(JSON.stringify(out), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  fn.calls = calls;
  return fn;
}

export const errorResponse = (status, code, message) =>
  new Response(JSON.stringify({ detail: { code, message } }), { status, headers: { "Content-Type": "application/json" } });

export const CLIENT = { actor: "key:abc", role: "client", owner_ref: "acme", key_id: 1, two_factor: false, two_factor_required: false,
  permissions: ["messages:send", "messages:read", "wallet:read", "sender:request", "contacts:read", "contacts:write", "campaigns:read", "email:read", "events:read", "keys:self", "portal:read", "billing:read", "reports:read", "inbox:read"] };
export const STAFF = { actor: "key:def", role: "superadmin", owner_ref: null, key_id: 2, two_factor: false, two_factor_required: false, permissions: ["*"] };
