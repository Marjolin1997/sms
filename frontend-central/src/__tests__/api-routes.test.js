import { describe, expect, it, vi } from "vitest";
import { ApiError, api, errorKind, getToken, login, normalizePage, setSessionExpiredHandler, setToken } from "../api.js";
import { actionsFor, adaptDecision, adaptEffective, adaptPolicyChange, adaptSender } from "../governance/model.js";
import { buildHash, canWrite, parseHash, resolveRoute } from "../routes.js";
import { ADMIN, OPERATOR, decision, effective, errorResponse, mockFetch, policy, sender } from "../test/helpers.js";

describe("API client", () => {
  it.each([[0, "network"], [400, "validation"], [422, "validation"], [401, "unauthenticated"], [403, "forbidden"], [404, "not_found"], [409, "conflict"], [429, "rate_limited"], [500, "server"]])("status %s → %s", (s, k) => expect(errorKind(s)).toBe(k));
  it("never uses the Enterprise API: only /auth and /admin paths are called by pages, bearer token only after login", async () => {
    setToken("tok");
    const f = mockFetch({ "GET /admin/senders": { items: [] } });
    await api.page("/admin/senders", { limit: 25, offset: 50, status: "pending" });
    expect(f.calls[0].url).toBe("/admin/senders?limit=25&offset=50&status=pending");
    expect(f.calls[0].headers.Authorization).toBe("Bearer tok");
    expect(f.calls[0].url).not.toContain("/v1/");
    expect(f.calls[0].url).not.toContain("tok");
  });
  it("login stores the token, never the password, and rolls back when /auth/me fails", async () => {
    mockFetch({ "POST /auth/token": { access_token: "t1" }, "GET /auth/me": errorResponse(500, "weird", "x") });
    await expect(login("a@b.c", "pw")).rejects.toBeInstanceOf(ApiError);
    expect(getToken()).toBe("");
    mockFetch({ "POST /auth/token": { access_token: "t1" }, "GET /auth/me": ADMIN });
    await login("a@b.c", "pw");
    expect(getToken()).toBe("t1");
    expect(JSON.stringify([sessionStorage, localStorage])).not.toContain("pw");
  });
  it("domain messages are translated and unknown 5xx text is flagged unsafe", async () => {
    mockFetch({ "GET /a": errorResponse(409, "conflict", "cannot approve from status approved"), "GET /b": errorResponse(500, "weird", "Traceback") });
    const e = await api.get("/a").catch((x) => x);
    expect(e.message).toBe("This action isn't possible while the sender is approved. The details were refreshed.");
    const s = await api.get("/b").catch((x) => x);
    expect(s.kind).toBe("server"); expect(s.friendly).toBe(false);
  });
  it("401 fires the expiry handler only with a token and for authenticated calls; 403 never does", async () => {
    const h = vi.fn(); setSessionExpiredHandler(h);
    mockFetch({ "GET /x": errorResponse(401, "unauthorized", "x"), "GET /y": errorResponse(403, "forbidden", "x") });
    setToken("t"); await api.get("/y").catch(() => {}); expect(h).not.toHaveBeenCalled();
    await api.get("/x").catch(() => {}); expect(h).toHaveBeenCalledTimes(1);
    setToken(""); await api.get("/x").catch(() => {}); expect(h).toHaveBeenCalledTimes(1);
    setSessionExpiredHandler(null);
  });
  it("timeout and abort", async () => {
    vi.stubGlobal("fetch", vi.fn((_u, init) => new Promise((_, rej) => init.signal.addEventListener("abort", () => rej(new DOMException("a", "AbortError"))))));
    await expect(api.get("/slow", {}, { timeoutMs: 5 })).rejects.toMatchObject({ code: "timeout", kind: "network" });
    const c = new AbortController(); const p = api.get("/slow", {}, { signal: c.signal }); c.abort();
    expect((await p.catch((e) => e)).name).toBe("AbortError");
  });
  it("normalizePage", () => expect(normalizePage({ items: [1], limit: 10, offset: 5, has_more: true })).toEqual({ items: [1], hasMore: true, limit: 10, offset: 5 }));
});

describe("routes", () => {
  it("parse/build round-trip and rejects hostile ids", () => {
    expect(parseHash("#senders?status=pending&offset=50")).toEqual({ id: "senders", rest: [], query: { status: "pending", offset: "50" } });
    expect(buildHash("senders", ["abc"], { status: "pending", x: "" })).toBe("#senders/abc?status=pending");
    expect(parseHash("#<script>").id).toBeNull();
  });
  it("resolveRoute: uuid-only detail ids, no detail for the overview", () => {
    expect(resolveRoute("#senders/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa").status).toBe("ok");
    expect(resolveRoute("#senders/xyz").status).toBe("not_found");
    expect(resolveRoute("#dashboard/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa").status).toBe("not_found");
    expect(resolveRoute("#nope").status).toBe("not_found");
  });
  it("only admins can write (UI affordance; server enforces)", () => { expect(canWrite(ADMIN)).toBe(true); expect(canWrite(OPERATOR)).toBe(false); expect(canWrite(null)).toBe(false); });
});

describe("adapters", () => {
  it("maps API shapes 1:1 and never calculates policy", () => {
    expect(adaptSender(sender({ status: "approved" }))).toMatchObject({ status: "approved", kind: "alphanumeric", displayValue: "ACME" });
    expect(adaptDecision(decision({ decided_by_id: "u", actor_label: null })).system).toBe(false);
    expect(adaptDecision(decision({ actor_label: "system:sender-policy" })).system).toBe(true);
    expect(adaptEffective(effective({ source: "default" }))).toMatchObject({ source: "default", allowed: true, requiresApproval: true, revision: null });
    expect(adaptPolicyChange({ ...policy(), created: false, revoked_senders: 2 })).toMatchObject({ created: false, revokedSenders: 2 });
  });
  it("action affordances follow the documented state machine; unknown states offer none", () => {
    expect(actionsFor("pending")).toEqual(["approve", "reject"]);
    expect(actionsFor("approved")).toEqual(["revoke"]);
    expect(actionsFor("rejected")).toEqual(["resubmit"]);
    expect(actionsFor("revoked")).toEqual(["resubmit"]);
    expect(actionsFor("weird")).toEqual([]);
  });
});
