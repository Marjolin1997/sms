import { describe, expect, it, vi } from "vitest";
import { ApiError, api, errorKind, normalizePage, setKey, setSessionExpiredHandler } from "../api.js";
import { errorResponse, mockFetch } from "../test/helpers.js";

describe("error classification", () => {
  it.each([[0, "network"], [400, "validation"], [422, "validation"], [401, "unauthenticated"], [403, "forbidden"], [404, "not_found"], [409, "conflict"], [429, "rate_limited"], [500, "server"], [503, "server"], [418, "other"]])(
    "status %s → %s", (status, kind) => expect(errorKind(status)).toBe(kind));

  it("exposes kind through real requests and distinguishes 401 from 403", async () => {
    setKey("k");
    mockFetch({ "GET /v1/a": errorResponse(401, "unauthorized", "x"), "GET /v1/b": errorResponse(403, "forbidden", "x"), "GET /v1/c": errorResponse(409, "conflict", "x") });
    await expect(api.get("/v1/a")).rejects.toMatchObject({ kind: "unauthenticated" });
    await expect(api.get("/v1/b")).rejects.toMatchObject({ kind: "forbidden" });
    await expect(api.get("/v1/c")).rejects.toMatchObject({ kind: "conflict", retryable: false });
  });
  it("429 carries Retry-After and is retryable", async () => {
    mockFetch({ "GET /v1/r": new Response(JSON.stringify({ detail: { code: "rate_limited", message: "slow" } }), { status: 429, headers: { "Retry-After": "7" } }) });
    await expect(api.get("/v1/r")).rejects.toMatchObject({ kind: "rate_limited", retryAfter: 7, retryable: true });
  });
  it("422 field errors are mapped per field (server stays authoritative)", async () => {
    mockFetch({ "POST /v1/f": new Response(JSON.stringify({ detail: [{ loc: ["body", "value"], msg: "too short" }, { loc: ["body", "country"], msg: "bad" }] }), { status: 422 }) });
    const e = await api.post("/v1/f", {}).catch((x) => x);
    expect(e.kind).toBe("validation");
    expect(e.fields).toEqual({ value: "too short", country: "bad" });
  });
  it("network failure is a retryable 'network' error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("failed"); }));
    await expect(api.get("/v1/x")).rejects.toMatchObject({ kind: "network", retryable: true });
  });
  it("timeouts become a timeout error, caller aborts do not become user errors", async () => {
    vi.stubGlobal("fetch", vi.fn((_u, init) => new Promise((_, rej) => init.signal.addEventListener("abort", () => rej(new DOMException("aborted", "AbortError"))))));
    await expect(api.get("/v1/slow", {}, { timeoutMs: 10 })).rejects.toMatchObject({ kind: "network", code: "timeout" });
    const ctl = new AbortController();
    const p = api.get("/v1/slow", {}, { signal: ctl.signal });
    ctl.abort();
    const e = await p.catch((x) => x);
    expect(e).not.toBeInstanceOf(ApiError);
    expect(e.name).toBe("AbortError");
  });
});

describe("sender domain codes", () => {
  it.each(["sender_authority_frozen", "sender_policy_denied", "sender_revoked", "not_synchronized"])("%s has a friendly message and hides server detail", async (code) => {
    mockFetch({ "POST /v1/sender-ids": errorResponse(409, code, "Traceback (most recent call last): internal detail") });
    const e = await api.post("/v1/sender-ids", {}).catch((x) => x);
    expect(e.friendly).toBe(true);
    expect(e.message).not.toMatch(/Traceback|internal/);
  });
});

describe("session expiry", () => {
  it("fires the handler on 401 during a session, not on 403, and not without a key", async () => {
    const h = vi.fn();
    setSessionExpiredHandler(h);
    mockFetch({ "GET /v1/a": errorResponse(401, "unauthorized", "x"), "GET /v1/b": errorResponse(403, "forbidden", "x") });
    setKey("k");
    await api.get("/v1/b").catch(() => {});
    expect(h).not.toHaveBeenCalled();
    await api.get("/v1/a").catch(() => {});
    expect(h).toHaveBeenCalledTimes(1);
    setKey("");
    await api.get("/v1/a").catch(() => {});
    expect(h).toHaveBeenCalledTimes(1);
    setSessionExpiredHandler(null);
  });
});

describe("normalizePage()", () => {
  it("handles Central pages, Enterprise cursor pages and plain arrays", () => {
    expect(normalizePage({ items: [1], limit: 10, offset: 20, has_more: true })).toEqual({ items: [1], hasMore: true, limit: 10, offset: 20 });
    expect(normalizePage({ items: [1], next_before_id: 5 }, 50, 0).hasMore).toBe(true);
    expect(normalizePage([1, 2], 2, 0).hasMore).toBe(true);
    expect(normalizePage([1], 50, 0).hasMore).toBe(false);
    expect(normalizePage(null).items).toEqual([]);
  });
  it("api.page() sends limit/offset and filters", async () => {
    setKey("k");
    const f = mockFetch({ "GET /admin/senders": { items: [], limit: 5, offset: 10, has_more: false } });
    const p = await api.page("/admin/senders", { limit: 5, offset: 10, status: "pending" });
    expect(f.calls[0].url).toBe("/admin/senders?limit=5&offset=10&status=pending");
    expect(p).toMatchObject({ limit: 5, offset: 10, hasMore: false });
  });
});
