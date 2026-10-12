import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import App from "../App.jsx";
import { setKey } from "../api.js";
import { I18nProvider, LangSwitch } from "../i18n.jsx";
import Approvals from "../pages/Approvals.jsx";
import { SenderDetail, SenderList } from "../senders/components.jsx";
import { CLIENT, STAFF, errorResponse, mockFetch } from "../test/helpers.js";
import { UiProvider } from "../ui.jsx";

const APPROVER = { actor: "key:ap", role: "approver", owner_ref: null, permissions: ["sender:review", "template:review", "monitor:read"] };
const SUPPORT = { actor: "key:su", role: "support", owner_ref: null, permissions: ["wallet:read", "messages:read", "monitor:read"] };
const WILDCARD_CLIENT = { ...CLIENT, permissions: ["*"] };

let n = 100;
const row = (o = {}) => ({ id: ++n, owner_ref: "acme", country: "AL", value: `SND${n}`, kind: "alphanumeric", status: "approved", reason: null, created_at: "2026-01-01T00:00:00Z", reviewed_by: "ops",
  authority_mode: "local", effective_status: "approved", sync_status: "not_applicable", can_resubmit: false, can_review_locally: true, ...o });
const wrap = (ui) => render(<I18nProvider><UiProvider>{ui}</UiProvider></I18nProvider>);
const statusCell = (name) => screen.getByText(name).closest("tr");
const badge = (txt) => document.querySelector(".badge") && [...document.querySelectorAll(".badge")].find((b) => b.textContent.includes(txt));

describe("list: effective status is the server's, never derived", () => {
  it("1/2/3 renders effective status for local approved and pending", async () => {
    mockFetch({ "GET /v1/sender-ids": [row({ value: "APPR" }), row({ value: "PEND", status: "pending", effective_status: "pending" })] });
    wrap(<SenderList me={CLIENT} />);
    expect(await screen.findByText("APPR")).toBeInTheDocument();
    expect(within(statusCell("APPR")).getByText("approved")).toHaveClass("good");
    expect(within(statusCell("PEND")).getByText("pending")).toHaveClass("warn");
  });
  it("4 shadow keeps the local/effective status as primary and labels the authority", async () => {
    mockFetch({ "GET /v1/sender-ids": [row({ value: "SHD", authority_mode: "shadow", status: "pending", effective_status: "pending", sync_status: "synchronized" })] });
    wrap(<SenderList me={CLIENT} />);
    const tr = (await screen.findByText("SHD")).closest("tr");
    expect(within(tr).getByText("pending")).toBeInTheDocument();
    expect(within(tr).getByText("Local (Central verification active)")).toBeInTheDocument();
  });
  it("5 central approved overrides local pending visually (customer sees only the effective status)", async () => {
    mockFetch({ "GET /v1/sender-ids": [row({ value: "CEN", authority_mode: "central", status: "pending", effective_status: "approved", central_status: "approved", sync_status: "synchronized", can_review_locally: false })] });
    wrap(<SenderList me={CLIENT} />);
    const tr = (await screen.findByText("CEN")).closest("tr");
    expect(within(tr).getByText("approved")).toHaveClass("good");
    expect(within(tr).queryByText("pending")).toBeNull();
    expect(within(tr).getByText("Central")).toBeInTheDocument();
  });
  it("5b staff additionally sees the local record as secondary text", async () => {
    mockFetch({ "GET /v1/sender-ids": [row({ value: "CEN", authority_mode: "central", status: "pending", effective_status: "approved", sync_status: "synchronized", can_review_locally: false })] });
    wrap(<SenderList me={STAFF} />);
    expect(await screen.findByText("Local record: pending")).toBeInTheDocument();
  });
  it("6 central revoked overrides local approved (never shown as approved)", async () => {
    mockFetch({ "GET /v1/sender-ids": [row({ value: "REV", authority_mode: "central", status: "approved", effective_status: "revoked", central_status: "revoked", sync_status: "synchronized", can_review_locally: false })] });
    wrap(<SenderList me={CLIENT} />);
    const tr = (await screen.findByText("REV")).closest("tr");
    expect(within(tr).getByText("revoked")).toHaveClass("bad");
    expect(within(tr).queryByText("approved")).toBeNull();
  });
  it("7 not_synchronized is never shown as approved (even if the local record says approved)", async () => {
    mockFetch({ "GET /v1/sender-ids": [row({ value: "NSY", authority_mode: "central", status: "approved", effective_status: "not_synchronized", sync_status: "not_synchronized", can_review_locally: false })] });
    wrap(<SenderList me={CLIENT} />);
    const tr = (await screen.findByText("NSY")).closest("tr");
    expect(within(tr).getAllByText("not synchronized").length).toBe(2); // status + sync
    expect(within(tr).queryByText("approved")).toBeNull();
  });
  it("8 stale keeps the server's effective status and adds a stale badge", async () => {
    mockFetch({ "GET /v1/sender-ids": [row({ value: "STL", authority_mode: "central", effective_status: "approved", sync_status: "stale", can_review_locally: false })] });
    wrap(<SenderList me={CLIENT} />);
    const tr = (await screen.findByText("STL")).closest("tr");
    expect(within(tr).getAllByText("approved").length).toBeGreaterThan(0);
    expect(tr.querySelectorAll(".badge.warn").length).toBeGreaterThan(0);
    expect(within(tr).queryByText("rejected")).toBeNull();
  });
  it("9 unknown statuses render neutral and do not crash", async () => {
    mockFetch({ "GET /v1/sender-ids": [row({ value: "UNK", effective_status: "something_new", sync_status: "weird", authority_mode: "future" })] });
    wrap(<SenderList me={CLIENT} />);
    const tr = (await screen.findByText("UNK")).closest("tr");
    expect(within(tr).getByText("something new")).toHaveClass("muted");
    expect(within(tr).getByText("future")).toBeInTheDocument();
  });
});

describe("resubmit / local review are controlled only by server flags", () => {
  it("13/14 can_resubmit decides (status 'rejected' alone shows nothing)", async () => {
    mockFetch({ "GET /v1/sender-ids": [row({ value: "YES", status: "rejected", effective_status: "rejected", can_resubmit: true }), row({ value: "NOPE", status: "rejected", effective_status: "rejected", can_resubmit: false })] });
    wrap(<SenderList me={CLIENT} />);
    await screen.findByText("YES");
    expect(within(statusCell("YES")).getByRole("button", { name: /Submit YES \(AL\) again/ })).toBeInTheDocument();
    expect(within(statusCell("NOPE")).queryByRole("button", { name: /again/ })).toBeNull();
  });
  it("15 resubmit posts, refetches, and does not change the status locally", async () => {
    const user = userEvent.setup();
    let gets = 0; let posted;
    const f = mockFetch({
      "GET /v1/sender-ids": () => { gets++; return [row({ id: 7, value: "RES", status: "rejected", effective_status: "rejected", can_resubmit: gets < 2 })]; },
      "POST /v1/sender-ids": (r) => { posted = r.body; return row({ id: 7, status: "pending" }); },
    });
    wrap(<SenderList me={CLIENT} />);
    await user.click(await screen.findByRole("button", { name: /Submit RES \(AL\) again/ }));
    await user.click(screen.getByRole("button", { name: "Submit again", hidden: false, description: undefined }).closest(".dialog") ? within(screen.getByRole("dialog")).getByRole("button", { name: "Submit again" }) : screen.getByRole("button", { name: "Submit again" }));
    await waitFor(() => expect(gets).toBeGreaterThanOrEqual(2));
    expect(posted).toEqual({ owner_ref: "acme", country: "AL", value: "RES" });
    expect(f.calls.filter((c) => c.method === "GET").length).toBeGreaterThanOrEqual(2);
    await waitFor(() => expect(screen.queryByRole("button", { name: /Submit RES/ })).toBeNull());
  });
  it("10/36 customers (and wildcard customer keys) never get review controls", async () => {
    mockFetch({ "GET /v1/sender-ids/7": row({ id: 7, value: "DET", status: "pending", effective_status: "pending" }) });
    const { unmount } = wrap(<SenderDetail me={CLIENT} id="7" />);
    await screen.findByRole("heading", { name: "DET" });
    expect(screen.queryByRole("button", { name: /Approve|Reject|Revoke/ })).toBeNull();
    unmount();
    mockFetch({ "GET /v1/sender-ids/7": row({ id: 7, value: "DET", status: "pending", effective_status: "pending" }) });
    wrap(<SenderDetail me={WILDCARD_CLIENT} id="7" />);
    await screen.findByRole("heading", { name: "DET" });
    expect(screen.queryByRole("button", { name: /Approve|Reject|Revoke/ })).toBeNull();
    expect(screen.queryByText("Diagnostics")).toBeNull();
  });
  it("11 approver sees review controls only when can_review_locally=true", async () => {
    mockFetch({ "GET /v1/sender-ids": [row({ value: "LOC", status: "pending", effective_status: "pending", can_review_locally: true })], "GET /v1/templates": [] });
    wrap(<Approvals me={APPROVER} />);
    expect(await screen.findByRole("button", { name: /Approve LOC/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Reject LOC/ })).toBeInTheDocument();
  });
  it("12 central mode hides local review and says approvals are managed centrally", async () => {
    mockFetch({ "GET /v1/sender-ids": [row({ value: "CEN", authority_mode: "central", status: "pending", effective_status: "pending", can_review_locally: false })], "GET /v1/templates": [] });
    wrap(<Approvals me={APPROVER} />);
    await screen.findByText("CEN");
    expect(screen.queryByRole("button", { name: /Approve CEN|Reject CEN/ })).toBeNull();
    expect(screen.getAllByText("Sender approvals are managed centrally.").length).toBeGreaterThan(0);
  });
  it("23 approve posts without owner_ref, refetches and never marks approved itself", async () => {
    const user = userEvent.setup();
    let gets = 0; let body;
    mockFetch({ "GET /v1/sender-ids": () => { gets++; return gets === 1 ? [row({ id: 9, value: "APR", status: "pending", effective_status: "pending" })] : []; }, "GET /v1/templates": [], "POST /v1/sender-ids/9/approve": (r) => { body = r.body; return row({ id: 9 }); } });
    wrap(<Approvals me={APPROVER} />);
    await user.click(await screen.findByRole("button", { name: /Approve APR/ }));
    await waitFor(() => expect(gets).toBeGreaterThanOrEqual(2));
    expect(body).toEqual({});
  });
  it("21/22 reject and revoke require a reason, which is sent to the server", async () => {
    const user = userEvent.setup();
    const bodies = {};
    mockFetch({
      "GET /v1/sender-ids/5": row({ id: 5, value: "RJ", status: "pending", effective_status: "pending" }),
      "POST /v1/sender-ids/5/reject": (r) => { bodies.reject = r.body; return row({ id: 5, status: "rejected" }); },
      "GET /v1/sender-ids/6": row({ id: 6, value: "RV", status: "approved", effective_status: "approved" }),
      "POST /v1/sender-ids/6/revoke": (r) => { bodies.revoke = r.body; return row({ id: 6, status: "revoked" }); },
    });
    const a = wrap(<SenderDetail me={STAFF} id="5" />);
    await user.click(await screen.findByRole("button", { name: /Reject RJ/ }));
    const dlg = screen.getByRole("dialog");
    expect(within(dlg).getByRole("button", { name: "Reject" })).toBeDisabled();
    await user.type(within(dlg).getByLabelText("Reason"), "Brand mismatch");
    await user.click(within(dlg).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(bodies.reject).toEqual({ reason: "Brand mismatch" }));
    a.unmount();
    wrap(<SenderDetail me={STAFF} id="6" />);
    await user.click(await screen.findByRole("button", { name: /Revoke RV/ }));
    await user.type(within(screen.getByRole("dialog")).getByLabelText("Reason"), "Policy breach");
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(bodies.revoke).toEqual({ reason: "Policy breach" }));
  });
  it("24/19 sender_authority_frozen race: friendly message and a refetch", async () => {
    const user = userEvent.setup();
    let gets = 0;
    mockFetch({ "GET /v1/sender-ids/9": () => { gets++; return row({ id: 9, value: "RACE", status: "pending", effective_status: "pending", can_review_locally: gets < 2, authority_mode: gets < 2 ? "local" : "central" }); },
      "POST /v1/sender-ids/9/approve": errorResponse(409, "sender_authority_frozen", "Traceback internal") });
    wrap(<SenderDetail me={STAFF} id="9" />);
    await user.click(await screen.findByRole("button", { name: /Approve RACE/ }));
    expect(await screen.findByText(/managed centrally now/)).toBeInTheDocument();
    expect(screen.queryByText(/Traceback/)).toBeNull();
    await waitFor(() => expect(gets).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(screen.queryByRole("button", { name: /Approve RACE/ })).toBeNull());
  });
});

describe("request form and field errors", () => {
  it("16/17 success path; button disabled until 3 chars", async () => {
    const user = userEvent.setup();
    let posted; let gets = 0;
    mockFetch({ "GET /v1/sender-ids": () => { gets++; return []; }, "POST /v1/sender-ids": (r) => { posted = r.body; return row({ status: "pending" }); } });
    wrap(<SenderList me={CLIENT} />);
    const input = await screen.findByLabelText(/^Sender ID/);
    const submit = screen.getByRole("button", { name: "Submit for approval" });
    expect(submit).toBeDisabled();
    await user.type(input, "AC");
    expect(screen.getByText("Use at least 3 characters")).toBeInTheDocument();
    await user.type(input, "ME");
    await user.click(submit);
    await waitFor(() => expect(posted).toEqual({ country: "AL", value: "ACME" }));
    await waitFor(() => expect(gets).toBeGreaterThanOrEqual(2));
    expect(input).toHaveValue("");
  });
  it("18 server field errors appear next to the field, focus it, and clear on edit", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /v1/sender-ids": [], "POST /v1/sender-ids": new Response(JSON.stringify({ detail: [{ loc: ["body", "value"], msg: "String should have at most 16 characters" }] }), { status: 422 }) });
    wrap(<SenderList me={CLIENT} />);
    const input = await screen.findByLabelText(/^Sender ID/);
    await user.type(input, "ABCDEF");
    await user.click(screen.getByRole("button", { name: "Submit for approval" }));
    expect(await screen.findByText("String should have at most 16 characters")).toBeInTheDocument();
    await waitFor(() => expect(input).toHaveFocus());
    expect(input).toHaveAttribute("aria-invalid", "true");
    await user.type(input, "X");
    expect(screen.queryByText("String should have at most 16 characters")).toBeNull();
  });
  it("18b domain code invalid_sender is shown on the Sender ID field", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /v1/sender-ids": [], "POST /v1/sender-ids": errorResponse(422, "invalid_sender", "x") });
    wrap(<SenderList me={CLIENT} />);
    await user.type(await screen.findByLabelText(/^Sender ID/), "!!!");
    await user.click(screen.getByRole("button", { name: "Submit for approval" }));
    expect(await screen.findByText(/isn't valid/)).toBeInTheDocument();
  });
  it.each([
    ["sender_authority_frozen", 409, /managed centrally now/],
    ["sender_policy_denied", 403, /not allowed for that country/],
    ["sender_revoked", 409, /was revoked/],
    ["sender_rejected", 409, /was rejected/],
    ["sender_pending", 409, /still waiting/],
    ["not_synchronized", 409, /not synchronized yet/],
    ["conflict", 409, /conflicts with what already exists/],
  ])("19/20 %s shows a user-safe message (no internals)", async (code, status, re) => {
    const user = userEvent.setup();
    mockFetch({ "GET /v1/sender-ids": [], "POST /v1/sender-ids": errorResponse(status, code, "Traceback (most recent call last)") });
    wrap(<SenderList me={CLIENT} />);
    await user.type(await screen.findByLabelText(/^Sender ID/), "ACME");
    await user.click(screen.getByRole("button", { name: "Submit for approval" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(re);
    expect(alert).not.toHaveTextContent("Traceback");
  });
  it.each([[429, "rate_limited", /too fast/], [503, "weird", /Something went wrong on our side/]])("32/33 status %s is sanitized", async (status, code, re) => {
    const user = userEvent.setup();
    mockFetch({ "GET /v1/sender-ids": [], "POST /v1/sender-ids": errorResponse(status, code, "db password=hunter2") });
    wrap(<SenderList me={CLIENT} />);
    await user.type(await screen.findByLabelText(/^Sender ID/), "ACME");
    await user.click(screen.getByRole("button", { name: "Submit for approval" }));
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.some((a) => re.test(a.textContent))).toBe(true);
    expect(document.body.textContent).not.toContain("hunter2");
  });
  it("31 network failure on submit is a readable error", async () => {
    const user = userEvent.setup();
    const f = mockFetch({ "GET /v1/sender-ids": [] });
    wrap(<SenderList me={CLIENT} />);
    await user.type(await screen.findByLabelText(/^Sender ID/), "ACME");
    globalThis.fetch.mockImplementationOnce(async () => { throw new TypeError("offline"); });
    await user.click(screen.getByRole("button", { name: "Submit for approval" }));
    expect((await screen.findAllByText(/Can't reach the server/)).length).toBeGreaterThan(0);
    expect(f).toBeDefined();
  });
});

describe("filters, pagination, empty, loading, errors", () => {
  const many = (k) => Array.from({ length: k }, (_, i) => row({ value: `S${String(i).padStart(3, "0")}`, country: i % 2 ? "XK" : "AL", kind: i % 3 ? "alphanumeric" : "numeric", effective_status: i % 4 ? "approved" : "pending" }));
  it("25 filters by search, status, country and type (over the loaded, capped set)", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /v1/sender-ids": [row({ value: "ALPHA", country: "AL" }), row({ value: "BETA", country: "XK", effective_status: "pending" }), row({ value: "GAMMA", country: "XK", kind: "numeric" })] });
    wrap(<SenderList me={CLIENT} />);
    await screen.findByText("ALPHA");
    await user.type(screen.getByRole("searchbox", { name: "Search sender IDs" }), "bet");
    expect(screen.queryByText("ALPHA")).toBeNull();
    expect(screen.getByText("BETA")).toBeInTheDocument();
    await user.clear(screen.getByRole("searchbox"));
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "pending");
    expect(screen.getByText("BETA")).toBeInTheDocument(); expect(screen.queryByText("GAMMA")).toBeNull();
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "");
    await user.selectOptions(screen.getByRole("combobox", { name: "Country" }), "XK");
    expect(screen.queryByText("ALPHA")).toBeNull();
    await user.selectOptions(screen.getByRole("combobox", { name: "Type" }), "numeric");
    expect(screen.getByText("GAMMA")).toBeInTheDocument(); expect(screen.queryByText("BETA")).toBeNull();
  });
  it("26 paginates the loaded rows (20 per page)", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /v1/sender-ids": many(45) });
    wrap(<SenderList me={CLIENT} />);
    await screen.findByText("S000");
    expect(screen.getAllByRole("row")).toHaveLength(21);
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByText("S020")).toBeInTheDocument();
    expect(screen.queryByText("S000")).toBeNull();
  });
  it("27/28 distinct empty states with the right call to action", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /v1/sender-ids": [] });
    const a = wrap(<SenderList me={CLIENT} />);
    expect(await screen.findByText("No sender IDs yet")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Request a sender ID" }).length).toBeGreaterThan(0);
    a.unmount();
    mockFetch({ "GET /v1/sender-ids": [row({ value: "ONLY" })] });
    wrap(<SenderList me={CLIENT} />);
    await screen.findByText("ONLY");
    await user.type(screen.getByRole("searchbox"), "zzz");
    expect(screen.getByText("No sender IDs match your filters")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByText("ONLY")).toBeInTheDocument();
  });
  it("29 shows a loading skeleton first", async () => {
    mockFetch({ "GET /v1/sender-ids": () => new Promise(() => {}) });
    wrap(<SenderList me={CLIENT} />);
    expect(await screen.findByLabelText("Loading")).toBeInTheDocument();
  });
  it("30 error state with retry recovers", async () => {
    const user = userEvent.setup();
    let ok = false;
    mockFetch({ "GET /v1/sender-ids": () => (ok ? [row({ value: "BACK" })] : errorResponse(500, "weird", "boom secret")) });
    wrap(<SenderList me={CLIENT} />);
    const state = await screen.findByText("Something went wrong");
    expect(state).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("boom secret");
    ok = true;
    await user.click(screen.getAllByRole("button", { name: "Try again" })[0]);
    expect(await screen.findByText("BACK")).toBeInTheDocument();
  });
  it("31b network failure on load shows the offline state", async () => {
    mockFetch({});
    globalThis.fetch.mockImplementation(async () => { throw new TypeError("x"); });
    wrap(<SenderList me={CLIENT} />);
    expect(await screen.findByText("You seem to be offline")).toBeInTheDocument();
  });
  it("37 mobile: diagnostic columns are marked hide-sm, the rest carry data-labels", async () => {
    mockFetch({ "GET /v1/sender-ids": [row({ value: "MOB" })] });
    wrap(<SenderList me={CLIENT} />);
    const tr = (await screen.findByText("MOB")).closest("tr");
    const hidden = [...tr.querySelectorAll("td.hide-sm")].map((td) => td.dataset.label);
    expect(hidden).toEqual(["Type", "Sync", "Authority", "Requested"]);
    expect(tr.querySelector("td[data-label='Status']")).not.toBeNull();
  });
  it("38 keyboard: actions are reachable and operable with the keyboard", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /v1/sender-ids": [row({ value: "KBD", status: "rejected", effective_status: "rejected", can_resubmit: true })] });
    wrap(<SenderList me={CLIENT} />);
    const btn = await screen.findByRole("button", { name: /Submit KBD \(AL\) again/ });
    btn.focus();
    await user.keyboard("{Enter}");
    const dlg = await screen.findByRole("dialog");
    expect(dlg).toHaveAttribute("aria-modal", "true");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(btn).toHaveFocus();
  });
});

describe("detail", () => {
  it("34 customer detail shows server fields and no staff diagnostics", async () => {
    mockFetch({ "GET /v1/sender-ids/7": row({ id: 7, value: "DET", authority_mode: "central", status: "pending", effective_status: "approved", central_status: "approved", sync_status: "synchronized", can_review_locally: false }) });
    wrap(<SenderDetail me={CLIENT} id="7" />);
    await screen.findByRole("heading", { name: "DET" });
    expect(screen.getByText("Central")).toBeInTheDocument();
    expect(screen.getByText("approved")).toBeInTheDocument();
    expect(screen.queryByText("Diagnostics")).toBeNull();
    expect(screen.getByText("Decision history isn't available here yet.")).toBeInTheDocument();
  });
  it("35 staff detail adds local/central status, drift and revisions", async () => {
    mockFetch({ "GET /v1/sender-ids/7": row({ id: 7, value: "DET", authority_mode: "central", status: "approved", effective_status: "revoked", central_status: "revoked", sync_status: "synchronized", can_review_locally: false, drift: "local_allow_central_deny", cp_revision: 12, policy_revision: 3 }) });
    wrap(<SenderDetail me={STAFF} id="7" />);
    await screen.findByText("Diagnostics");
    expect(screen.getByText("local_allow_central_deny")).toBeInTheDocument();
    expect(screen.getByText("Local status").nextSibling).toHaveTextContent("approved");
    expect(screen.getByText("Central status").nextSibling).toHaveTextContent("revoked");
    expect(screen.getByText("Control-plane revision").nextSibling).toHaveTextContent("12");
    expect(screen.getByText("Policy revision").nextSibling).toHaveTextContent("3");
    expect(screen.getByText("Sender approvals are managed centrally.")).toBeInTheDocument();
  });
  it("7b not_synchronized detail: warning, refresh, no approval claim", async () => {
    mockFetch({ "GET /v1/sender-ids/7": row({ id: 7, value: "NSY", authority_mode: "central", status: "approved", effective_status: "not_synchronized", sync_status: "not_synchronized", can_review_locally: false }) });
    wrap(<SenderDetail me={CLIENT} id="7" />);
    expect(await screen.findByText(/hasn't been confirmed yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
    expect(screen.queryByText("approved")).toBeNull();
  });
  it("local-mode staff diagnostic sync_status=not_synchronized does NOT warn (effective status is approved)", async () => {
    mockFetch({ "GET /v1/sender-ids/7": row({ id: 7, value: "LOCSTAFF", authority_mode: "local", effective_status: "approved", sync_status: "not_synchronized" }) });
    wrap(<SenderDetail me={STAFF} id="7" />);
    await screen.findByRole("heading", { name: "LOCSTAFF" });
    expect(screen.queryByText(/hasn't been confirmed yet/)).toBeNull();
    expect(screen.getAllByText("approved").length).toBeGreaterThan(0);
  });
  it("8b stale detail: warning that does not claim denial", async () => {
    mockFetch({ "GET /v1/sender-ids/7": row({ id: 7, value: "STL", authority_mode: "central", effective_status: "approved", sync_status: "stale", can_review_locally: false }) });
    wrap(<SenderDetail me={CLIENT} id="7" />);
    expect(await screen.findByText(/older than usual/)).toBeInTheDocument();
    expect(screen.queryByText("rejected")).toBeNull();
  });
  it("detail load failure offers retry; 404 is not retryable", async () => {
    mockFetch({ "GET /v1/sender-ids/7": errorResponse(404, "not_found", "sender id not found") });
    wrap(<SenderDetail me={CLIENT} id="7" />);
    expect(await screen.findByText("We couldn't find that")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });
});

describe("roles and routing", () => {
  const mocks = { "GET /v1/portal/overview": { wallets: [], sms: {}, email: {}, campaigns: {}, webhooks: {} }, "GET /v1/portal/onboarding": { done: true, steps: [] }, "GET /v1/messages": { items: [], next_before_id: null }, "GET /v1/inbox/unread": { unread: 0 }, "GET /v1/admin/stats": { oldest_queued_age_seconds: null, stuck_sending: 0, dlr_problems_24h: {}, switches: [] }, "GET /v1/admin/accounts": [], "GET /v1/templates": [] };
  const boot = async (me, hash, extra = {}) => { setKey("k"); location.hash = hash; mockFetch({ "GET /v1/me": me, ...mocks, ...extra }); render(<App />); await screen.findByRole("navigation", { name: "Main navigation" }); };
  it("deep link #senders/7 opens the detail inside the app shell", async () => {
    await boot(CLIENT, "#senders/7", { "GET /v1/sender-ids/7": row({ id: 7, value: "DEEP" }) });
    expect(await screen.findByRole("heading", { name: "DEEP" })).toBeInTheDocument();
  });
  it("36 support/approver cannot open the customer Sender IDs page (403), approver can open Approvals", async () => {
    await boot(SUPPORT, "#senders");
    expect(await screen.findByText("You don't have access to this page")).toBeInTheDocument();
  });
  it("approver reaches Approvals and sees pending senders", async () => {
    await boot(APPROVER, "#approvals", { "GET /v1/sender-ids": [row({ value: "QUE", status: "pending", effective_status: "pending" })] });
    expect(await screen.findByRole("button", { name: /Approve QUE/ })).toBeInTheDocument();
  });
});

describe("i18n", () => {
  it("39/40 English and Albanian labels", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /v1/sender-ids": [row({ value: "I18", authority_mode: "shadow", effective_status: "pending", sync_status: "synchronized" })] });
    wrap(<><LangSwitch /><SenderList me={CLIENT} /></>);
    expect(await screen.findByText("Local (Central verification active)")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Shqip" }));
    expect(await screen.findByText("Lokal (verifikimi Central aktiv)")).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getByText("në pritje")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Autoriteti" })).toBeInTheDocument();
  });
});
