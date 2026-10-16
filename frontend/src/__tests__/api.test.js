import { describe, expect, it, vi } from "vitest";
import { ApiError, api, setKey, setOwner, setTotpPrompt } from "../api.js";
import { errorResponse, mockFetch } from "../test/helpers.js";

describe("api requests", () => {
  it("sends the bearer key and the selected account", async () => {
    setKey("sms_abc_secret"); setOwner("acme");
    const f = mockFetch({ "GET /v1/thing": { ok: 1 } });
    expect(await api.get("/v1/thing", { limit: 5 })).toEqual({ ok: 1 });
    expect(f.calls[0].url).toBe("/v1/thing?owner_ref=acme&limit=5");
    expect(f.calls[0].headers.Authorization).toBe("Bearer sms_abc_secret");
    setOwner("");
  });
  it("adds owner_ref to JSON bodies but not to arrays, and forwards custom headers", async () => {
    setKey("k"); setOwner("acme");
    const f = mockFetch({ "POST /v1/x": {}, "POST /v1/y": {} });
    await api.post("/v1/x", { a: 1 }, { headers: { "Idempotency-Key": "abc" } });
    expect(f.calls[0].body).toEqual({ owner_ref: "acme", a: 1 });
    expect(f.calls[0].headers["Idempotency-Key"]).toBe("abc");
    await api.post("/v1/y", [1, 2]);
    expect(f.calls[1].body).toEqual([1, 2]);
    setOwner("");
  });
  it("returns null for 204", async () => {
    mockFetch({ "DELETE /v1/x/1": new Response(null, { status: 204 }) });
    expect(await api.del("/v1/x/1")).toBeNull();
  });
});

describe("friendly errors", () => {
  it("maps known codes to plain language", async () => {
    mockFetch({ "GET /v1/a": errorResponse(402, "insufficient_funds", "insufficient available funds") });
    await expect(api.get("/v1/a")).rejects.toMatchObject({ status: 402, code: "insufficient_funds", message: expect.stringMatching(/balance/i) });
  });
  it("translates common server messages by pattern, with placeholders", async () => {
    mockFetch({ "GET /v1/b": errorResponse(422, "invalid", "at most 20 active keys per account") });
    await expect(api.get("/v1/b")).rejects.toThrow("You can have at most 20 active keys.");
  });
  it("passes unknown messages through and reports network failures", async () => {
    mockFetch({ "GET /v1/c": errorResponse(500, "weird", "something odd") });
    await expect(api.get("/v1/c")).rejects.toThrow("something odd");
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("offline"); }));
    await expect(api.get("/v1/d")).rejects.toMatchObject({ status: 0, code: "network" });
  });
  it("formats FastAPI validation errors", async () => {
    mockFetch({ "POST /v1/e": new Response(JSON.stringify({ detail: [{ loc: ["body", "to"], msg: "field required" }] }), { status: 422 }) });
    await expect(api.post("/v1/e", {})).rejects.toBeInstanceOf(ApiError);
  });
});

describe("two-factor step-up", () => {
  it("asks for a code, retries with X-TOTP and succeeds", async () => {
    setKey("k");
    const prompt = vi.fn(async () => "123456");
    setTotpPrompt(prompt);
    let n = 0;
    const f = mockFetch({ "POST /v1/admin/api-keys": (req) => (++n === 1 ? errorResponse(403, "totp_required", "code required") : { id: 1, seen: req.headers["X-TOTP"] }) });
    expect(await api.post("/v1/admin/api-keys", { name: "x" })).toEqual({ id: 1, seen: "123456" });
    expect(prompt).toHaveBeenCalledWith(false);
    expect(f.calls).toHaveLength(2);
    setTotpPrompt(null);
  });
  it("re-prompts (retry=true) on an invalid code and gives up after the user cancels", async () => {
    setKey("k");
    const answers = ["000000", ""];
    const prompt = vi.fn(async () => answers.shift());
    setTotpPrompt(prompt);
    let n = 0;
    mockFetch({ "POST /v1/z": () => errorResponse(403, n++ === 0 ? "totp_required" : "totp_invalid", "x") });
    await expect(api.post("/v1/z", {})).rejects.toMatchObject({ code: "totp_invalid" });
    expect(prompt.mock.calls.map((c) => c[0])).toEqual([false, true]);
    setTotpPrompt(null);
  });
});
