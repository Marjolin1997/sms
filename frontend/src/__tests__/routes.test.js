import { describe, expect, it } from "vitest";
import { NAV, can, parseHash, resolveRoute, visibleGroups } from "../routes.js";
import { CLIENT, STAFF } from "../test/helpers.js";

// Rolet reale të serverit (app/core/security.py ROLE_PERMS) — vetëm fixtura të /v1/me; serveri mbetet autoriteti.
const staff = (role, permissions) => ({ actor: `key:${role}`, role, owner_ref: null, permissions });
const APPROVER = staff("approver", ["sender:review", "template:review", "monitor:read"]);
const FINANCE = staff("finance", ["wallet:read", "wallet:write", "wallet:adjust", "topup:write", "topup:confirm", "audit:read", "monitor:read", "billing:read", "billing:admin", "reports:read", "wallet:alert"]);
const ids = (me) => visibleGroups(me).flatMap((g) => g.items.map((i) => i.id));

describe("route table", () => {
  it("has unique ids and a dynamic loader for every page", () => {
    const all = NAV.flatMap((g) => g.items);
    expect(new Set(all.map((i) => i.id)).size).toBe(all.length);
    expect(all.every((i) => typeof i.load === "function")).toBe(true);
  });
  it("can(): null perm is open, '*' is everything, 'a|b' needs one", () => {
    expect(can({ permissions: [] }, null)).toBe(true);
    expect(can({ permissions: ["*"] }, "x:y")).toBe(true);
    expect(can({ permissions: ["b"] }, "a|b")).toBe(true);
    expect(can({ permissions: ["c"] }, "a|b")).toBe(false);
    expect(can({}, "a")).toBe(false);
  });
});

describe("role-aware navigation", () => {
  it("customer: no Staff group, no staff pages", () => {
    const g = visibleGroups(CLIENT);
    expect(g.some((x) => x.group === "Staff")).toBe(false);
    for (const staffPage of ["approvals", "accounts", "rates", "finance", "providers", "security", "admin"]) expect(ids(CLIENT)).not.toContain(staffPage);
    expect(ids(CLIENT)).toEqual(expect.arrayContaining(["dashboard", "send", "senders", "wallet"]));
  });
  it("superadmin: Staff group comes right after Overview", () => {
    const g = visibleGroups(STAFF);
    expect(g[0].items[0].id).toBe("dashboard");
    expect(g[1].group).toBe("Staff");
    expect(ids(STAFF)).toEqual(expect.arrayContaining(["approvals", "rates", "finance", "admin"]));
  });
  it("approver (operator-like): only approvals + monitoring pages, never money/admin", () => {
    expect(ids(APPROVER)).toEqual(expect.arrayContaining(["approvals", "accounts", "providers", "security"]));
    for (const p of ["finance", "rates", "admin", "send", "wallet"]) expect(ids(APPROVER)).not.toContain(p);
  });
  it("finance: finance page but not approvals", () => {
    expect(ids(FINANCE)).toContain("finance");
    expect(ids(FINANCE)).not.toContain("approvals");
  });
  it("a customer key with staff-looking permissions still never gets the Staff group", () => {
    const sneaky = { ...CLIENT, permissions: ["*"] };
    expect(visibleGroups(sneaky).some((g) => g.group === "Staff")).toBe(false);
  });
});

describe("hash parsing (deep-link safety)", () => {
  it("defaults to the overview and keeps sub-path segments", () => {
    expect(parseHash("")).toEqual({ id: "dashboard", rest: [] });
    expect(parseHash("#senders/12?tab=x")).toEqual({ id: "senders", rest: ["12"] });
  });
  it("rejects anything that is not a plain route id", () => {
    for (const h of ["#../etc", "#<script>", "#javascript:alert(1)", "#A B", `#${"a".repeat(50)}`]) expect(parseHash(h).id).toBeNull();
  });
});

describe("resolveRoute()", () => {
  it("ok for an allowed page, with breadcrumbs", () => {
    const r = resolveRoute(CLIENT, "#senders");
    expect(r.status).toBe("ok");
    expect(r.crumbs.map((c) => c.label)).toEqual(["Setup", "Sender IDs & templates"]);
  });
  it("forbidden (not a silent redirect) for a real page the role cannot open", () => {
    expect(resolveRoute(CLIENT, "#approvals").status).toBe("forbidden");
    expect(resolveRoute(APPROVER, "#finance").status).toBe("forbidden");
  });
  it("not_found for unknown or malformed routes", () => {
    expect(resolveRoute(STAFF, "#nope").status).toBe("not_found");
    expect(resolveRoute(STAFF, "#<x>").status).toBe("not_found");
  });
});
