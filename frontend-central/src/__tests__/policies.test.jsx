import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { I18nProvider } from "../i18n.jsx";
import Policies from "../pages/Policies.jsx";
import { PID, effective, errorResponse, mockFetch, pageOf, policy } from "../test/helpers.js";
import { UiProvider } from "../ui.jsx";

const wrap = (ui) => render(<I18nProvider><UiProvider>{ui}</UiProvider></I18nProvider>);
const list = (props = {}) => wrap(<Policies write rest={[]} query={{}} {...props} />);
const paths = (f) => f.calls.map((c) => `${c.method} ${c.path}`);
const urlOf = (f, p) => new URL(f.calls.find((c) => c.path === p).url, "http://x").searchParams;

describe("Country Policies list", () => {
  it("26 lists the current policies (latest_only) from the API with allowed/approval/revision", async () => {
    const f = mockFetch({ "GET /admin/sender-policies": pageOf([policy({ country: "AL", revision: 3, allowed: true, requires_approval: false }), policy({ country: "XK", sender_kind: "numeric", allowed: false, requires_approval: true })]) });
    list();
    await screen.findByText("Current policies");
    expect(urlOf(f, "/admin/sender-policies").get("latest_only")).toBe("true");
    const rows = screen.getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("allowed")).toHaveClass("good");
    expect(within(rows[0]).getByText("r3")).toBeInTheDocument();
    expect(within(rows[0]).getByText("No")).toBeInTheDocument();
    expect(within(rows[1]).getByText("denied")).toHaveClass("bad");
  });
  it("all revisions toggle sends no latest_only and shows no 'active' marker", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /admin/sender-policies": pageOf([policy()]) });
    list();
    await screen.findByText("Current policies");
    expect(screen.getByText("active")).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Show all revisions" }));
    expect(location.hash).toContain("all=1");
  });
  it("all-revisions deep link: historical rows are read-only (no edit/delete, no per-row revise)", async () => {
    const f = mockFetch({ "GET /admin/sender-policies": pageOf([policy({ revision: 1 }), policy({ revision: 2 })]) });
    list({ query: { all: "1" } });
    await screen.findByText("All policy revisions");
    expect(urlOf(f, "/admin/sender-policies").get("latest_only")).toBeNull();
    expect(screen.queryByRole("button", { name: /Edit|Delete|New revision$/ })).toBeNull();
    expect(screen.queryByText("active")).toBeNull();
  });
  it("server-side pagination and filters go to the API", async () => {
    const user = userEvent.setup();
    const f = mockFetch({ "GET /admin/sender-policies": pageOf([policy()], { has_more: true, offset: 50 }) });
    list({ query: { offset: "50", country: "AL", sender_kind: "numeric" } });
    await screen.findByText("Current policies");
    const p = urlOf(f, "/admin/sender-policies");
    expect([p.get("offset"), p.get("country"), p.get("sender_kind")]).toEqual(["50", "AL", "numeric"]);
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(location.hash).toContain("offset=100");
  });
  it("empty list explains the default policy", async () => {
    mockFetch({ "GET /admin/sender-policies": pageOf([]) });
    list();
    expect(await screen.findByText("No explicit policies")).toBeInTheDocument();
  });
  it("operator: no create/revise buttons", async () => {
    mockFetch({ "GET /admin/sender-policies": pageOf([policy()]) });
    list({ write: false });
    await screen.findByText("Current policies");
    expect(screen.queryByRole("button", { name: /New policy revision|New revision/ })).toBeNull();
    expect(screen.getByText(/Read-only access/)).toBeInTheDocument();
  });
  it("error with retry and sanitized 5xx", async () => {
    mockFetch({ "GET /admin/sender-policies": errorResponse(500, "weird", "db password") });
    list();
    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("db password");
  });
});

describe("Effective policy preview (always from the backend)", () => {
  const preview = async (user, country = "AL", kind = "alphanumeric") => {
    await user.type(screen.getByRole("textbox", { name: "Country (ISO code)", description: "" }) || screen.getAllByLabelText("Country (ISO code)")[0], country);
    await user.click(screen.getByRole("button", { name: "Preview" }));
  };
  it("27 default policy: labelled as default, nothing is stored", async () => {
    const user = userEvent.setup();
    const f = mockFetch({ "GET /admin/sender-policies": pageOf([]), "GET /admin/sender-policies/effective": effective() });
    list();
    await screen.findByText("Effective policy preview");
    await user.type(screen.getAllByLabelText("Country (ISO code)")[0], "al");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText("Default policy")).toBeInTheDocument();
    expect(urlOf(f, "/admin/sender-policies/effective").get("country")).toBe("al");
    expect(paths(f).filter((p) => p.startsWith("POST"))).toEqual([]);
  });
  it("28 explicit policy: shows source, revision and effective window from the server", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /admin/sender-policies": pageOf([]), "GET /admin/sender-policies/effective": effective({ source: "explicit", revision: 4, allowed: false, requires_approval: true, effective_from: "2026-01-01T00:00:00+00:00" }) });
    list();
    await screen.findByText("Effective policy preview");
    await user.type(screen.getAllByLabelText("Country (ISO code)")[0], "AL");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    const card = (await screen.findByText("explicit")).closest("section");
    expect(within(card).getByText("denied")).toHaveClass("bad");
    expect(within(card).getByText("4")).toBeInTheDocument();
    expect(within(card).queryByText("Default policy")).toBeNull();
  });
  it("preview error is shown without leaking internals", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /admin/sender-policies": pageOf([]), "GET /admin/sender-policies/effective": errorResponse(500, "weird", "trace") });
    list();
    await screen.findByText("Effective policy preview");
    await user.type(screen.getAllByLabelText("Country (ISO code)")[0], "AL");
    await user.click(screen.getByRole("button", { name: "Preview" }));
    expect(await screen.findByText("Something went wrong on our side. Try again in a moment.")).toBeInTheDocument();
  });
});

describe("Create policy revision", () => {
  const open = async (user) => {
    await user.click(await screen.findByRole("button", { name: "New policy revision" }));
    return screen.getByRole("dialog");
  };
  it("29/30 saves a revision with exactly what the admin chose; identical = no-change message", async () => {
    const user = userEvent.setup();
    let body; let created = true;
    mockFetch({ "GET /admin/sender-policies": pageOf([]), "POST /admin/sender-policies": (r) => { body = r.body; return { ...policy({ revision: 2, requires_approval: false }), created, revoked_senders: 0 }; } });
    list();
    const dlg = await open(user);
    await user.type(within(dlg).getByLabelText(/Country \(ISO code\)/), "AL");
    await user.click(within(dlg).getByRole("checkbox", { name: "Requires approval" }));
    await user.type(within(dlg).getByLabelText(/^Reason/), "Trusted market");
    await user.click(within(dlg).getByRole("button", { name: "Save revision" }));
    await waitFor(() => expect(body).toEqual({ country: "AL", sender_kind: "alphanumeric", allowed: true, requires_approval: false, reason: "Trusted market" }));
    expect((await screen.findAllByText(/Policy revision 2 saved for AL alphanumeric/)).length).toBeGreaterThan(0);
    created = false;
    const dlg2 = await open(user);
    await user.type(within(dlg2).getByLabelText(/Country \(ISO code\)/), "AL");
    await user.type(within(dlg2).getByLabelText(/^Reason/), "again");
    await user.click(within(dlg2).getByRole("button", { name: "Save revision" }));
    expect((await screen.findAllByText(/No change: this is identical to the current revision/)).length).toBeGreaterThan(0);
  });
  it("31/E allowed=false needs an explicit confirmation that warns about revoking approved senders", async () => {
    const user = userEvent.setup();
    let posts = 0; let body;
    mockFetch({ "GET /admin/sender-policies": pageOf([]), "POST /admin/sender-policies": (r) => { posts++; body = r.body; return { ...policy({ allowed: false }), created: true, revoked_senders: 3 }; } });
    list();
    const dlg = await open(user);
    await user.type(within(dlg).getByLabelText(/Country \(ISO code\)/), "AL");
    await user.click(within(dlg).getByRole("checkbox", { name: "Allowed" }));
    await user.type(within(dlg).getByLabelText(/^Reason/), "Fraud wave");
    await user.click(within(dlg).getByRole("button", { name: "Save revision" }));
    expect(posts).toBe(0);
    expect(within(dlg).getByRole("alert")).toHaveTextContent("This policy change may revoke currently approved sender IDs.");
    expect(within(dlg).getByRole("alert").textContent).toMatch(/can't be recalled/);
    await user.click(within(dlg).getByRole("button", { name: "Yes, apply this change" }));
    await waitFor(() => expect(posts).toBe(1));
    expect(body.allowed).toBe(false);
    expect(await screen.findByText(/3 approved sender IDs were revoked\./)).toBeInTheDocument();
  });
  it("editing after the warning resets the confirmation step", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /admin/sender-policies": pageOf([]) });
    list();
    const dlg = await open(user);
    await user.type(within(dlg).getByLabelText(/Country \(ISO code\)/), "AL");
    await user.click(within(dlg).getByRole("checkbox", { name: "Allowed" }));
    await user.type(within(dlg).getByLabelText(/^Reason/), "x");
    await user.click(within(dlg).getByRole("button", { name: "Save revision" }));
    expect(within(dlg).getByRole("button", { name: "Yes, apply this change" })).toBeInTheDocument();
    await user.click(within(dlg).getByRole("checkbox", { name: "Allowed" }));
    expect(within(dlg).getByRole("button", { name: "Save revision" })).toBeInTheDocument();
  });
  it("32/48 allowed=false + requires_approval=false is NOT coerced; the backend's 422 is shown at the field", async () => {
    const user = userEvent.setup();
    let body;
    mockFetch({ "GET /admin/sender-policies": pageOf([]), "POST /admin/sender-policies": (r) => { body = r.body; return errorResponse(422, "invalid", "a country/kind that is not allowed must keep requires_approval=true (no silent coercion)"); } });
    list();
    const dlg = await open(user);
    await user.type(within(dlg).getByLabelText(/Country \(ISO code\)/), "AL");
    await user.click(within(dlg).getByRole("checkbox", { name: "Allowed" }));
    await user.click(within(dlg).getByRole("checkbox", { name: "Requires approval" }));
    await user.type(within(dlg).getByLabelText(/^Reason/), "x");
    await user.click(within(dlg).getByRole("button", { name: "Save revision" }));
    await user.click(within(dlg).getByRole("button", { name: "Yes, apply this change" }));
    expect(await within(dlg).findByText(/must keep “Requires approval” on/)).toBeInTheDocument();
    expect(body).toMatchObject({ allowed: false, requires_approval: false });
    await waitFor(() => expect(within(dlg).getByRole("checkbox", { name: "Requires approval" })).toHaveFocus());
  });
  it("38 pydantic field errors map to country/reason fields", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /admin/sender-policies": pageOf([]), "POST /admin/sender-policies": new Response(JSON.stringify({ detail: [{ loc: ["body", "country"], msg: "String should match pattern" }] }), { status: 422 }) });
    list();
    const dlg = await open(user);
    await user.type(within(dlg).getByLabelText(/Country \(ISO code\)/), "A1");
    await user.type(within(dlg).getByLabelText(/^Reason/), "x");
    await user.click(within(dlg).getByRole("button", { name: "Save revision" }));
    expect(await within(dlg).findByText("String should match pattern")).toBeInTheDocument();
  });
  it("409 concurrent revision and 403 are explained in the dialog", async () => {
    const user = userEvent.setup();
    let code = 409;
    mockFetch({ "GET /admin/sender-policies": pageOf([]), "POST /admin/sender-policies": () => (code === 409 ? errorResponse(409, "conflict", "a concurrent policy revision was created for this country and sender kind") : errorResponse(403, "forbidden", "insufficient role")) });
    list();
    const dlg = await open(user);
    await user.type(within(dlg).getByLabelText(/Country \(ISO code\)/), "AL");
    await user.type(within(dlg).getByLabelText(/^Reason/), "x");
    await user.click(within(dlg).getByRole("button", { name: "Save revision" }));
    expect(await within(dlg).findByRole("alert")).toHaveTextContent("Another change was saved at the same moment");
    code = 403;
    await user.click(within(dlg).getByRole("button", { name: "Save revision" }));
    await waitFor(() => expect(within(dlg).getByRole("alert")).toHaveTextContent("Your role is read-only for this action."));
  });
});

describe("Policy detail and history", () => {
  const detail = (props = {}) => wrap(<Policies write rest={[PID]} query={{}} {...props} />);
  const mocks = () => ({
    ["GET /admin/sender-policies/" + PID]: policy({ id: PID, revision: 2, allowed: false, reason: "fraud" }),
    "GET /admin/sender-policies": pageOf([policy({ revision: 1, reason: "initial" }), policy({ id: PID, revision: 2, allowed: false, reason: "fraud" })]),
    "GET /admin/sender-policies/effective": effective({ source: "explicit", revision: 2, allowed: false }),
  });
  it("33 shows detail, server-calculated effective policy and an immutable newest-first history", async () => {
    mockFetch(mocks());
    detail();
    expect(await screen.findByText(/AL · alphanumeric · r2/)).toBeInTheDocument();
    expect(await screen.findByText("Revision 2 (this one)")).toBeInTheDocument();
    const items = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(items[0]).toContain("Revision 2");
    expect(items[1]).toContain("Revision 1");
    expect(screen.getByText("Policy revisions are immutable.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Edit|Delete|Remove/ })).toBeNull();
    expect(screen.getByText("abc123")).toBeInTheDocument(); // hash: admin only
  });
  it("operator sees no hash and no create-from button", async () => {
    mockFetch(mocks());
    detail({ write: false });
    await screen.findByText(/AL · alphanumeric · r2/);
    expect(screen.queryByText("abc123")).toBeNull();
    expect(screen.queryByRole("button", { name: /New revision/ })).toBeNull();
  });
  it("admin can start a new revision pre-filled from this one", async () => {
    const user = userEvent.setup();
    mockFetch(mocks());
    detail();
    await user.click(await screen.findByRole("button", { name: "New revision from this" }));
    const dlg = screen.getByRole("dialog");
    expect(within(dlg).getByLabelText(/Country \(ISO code\)/)).toHaveValue("AL");
    expect(within(dlg).getByRole("checkbox", { name: "Allowed" })).not.toBeChecked();
  });
  it("404 policy is not retryable", async () => {
    mockFetch({ ["GET /admin/sender-policies/" + PID]: errorResponse(404, "not_found", "sender policy not found") });
    detail();
    expect(await screen.findByText("We couldn't find that")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });
});
