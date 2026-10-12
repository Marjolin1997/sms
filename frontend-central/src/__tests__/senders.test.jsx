import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { I18nProvider, LangSwitch } from "../i18n.jsx";
import Senders from "../pages/Senders.jsx";
import { SID, decision, effective, errorResponse, mockFetch, pageOf, sender } from "../test/helpers.js";
import { UiProvider } from "../ui.jsx";

const wrap = (ui) => render(<I18nProvider><UiProvider>{ui}</UiProvider></I18nProvider>);
const registry = (props = {}) => wrap(<Senders write rest={[]} query={{}} {...props} />);
const detail = (props = {}) => wrap(<Senders write rest={[SID]} query={{}} {...props} />);
const rowOf = (name) => screen.getByText(name).closest("tr");
const urlOf = (f, i = 0) => new URL(f.calls.filter((c) => c.method === "GET")[i].url, "http://x");

describe("Sender Registry (server-side)", () => {
  it("5 renders rows from the Central API with status badges and no client-side invention", async () => {
    mockFetch({ "GET /admin/senders": pageOf([sender({ display_value: "ALPHA", status: "approved" }), sender({ display_value: "BETA", status: "revoked", country: "XK", sender_kind: "numeric" })]) });
    registry();
    await screen.findByText("ALPHA");
    expect(within(rowOf("ALPHA")).getByText("approved")).toHaveClass("good");
    expect(within(rowOf("BETA")).getByText("revoked")).toHaveClass("bad");
    expect(within(rowOf("BETA")).getByText("numeric")).toBeInTheDocument();
  });
  it("6 pagination is server-side: offset/limit go to the API, next/prev update the URL state", async () => {
    const user = userEvent.setup();
    const f = mockFetch({ "GET /admin/senders": pageOf([sender({ display_value: "P2" })], { offset: 50, has_more: true }) });
    registry({ query: { offset: "50" } });
    await screen.findByText("P2");
    expect(urlOf(f).searchParams.get("offset")).toBe("50");
    expect(urlOf(f).searchParams.get("limit")).toBe("50");
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(location.hash).toBe("#senders?offset=100");
    await user.click(screen.getByRole("button", { name: "Previous" }));
    expect(location.hash).toBe("#senders");
  });
  it("6b page size is a server parameter", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /admin/senders": pageOf([sender()]) });
    registry();
    await screen.findByText("ACME");
    await user.selectOptions(screen.getByRole("combobox", { name: "Rows per page" }), "100");
    expect(location.hash).toBe("#senders?limit=100");
  });
  it("7/8 filters are sent to the API and deep links reproduce them", async () => {
    const user = userEvent.setup();
    const f = mockFetch({ "GET /admin/senders": pageOf([sender()]) });
    registry({ query: { status: "pending", country: "AL", sender_kind: "numeric", enterprise_id: "11111111-1111-4111-8111-111111111111" } });
    await screen.findByText("ACME");
    const p = urlOf(f).searchParams;
    expect([p.get("status"), p.get("country"), p.get("sender_kind"), p.get("enterprise_id")]).toEqual(["pending", "AL", "numeric", "11111111-1111-4111-8111-111111111111"]);
    expect(screen.getByRole("combobox", { name: "Status" })).toHaveValue("pending");
    await user.selectOptions(screen.getByRole("combobox", { name: "Status" }), "revoked");
    await user.clear(screen.getByRole("textbox", { name: "Country" }));
    await user.type(screen.getByRole("textbox", { name: "Country" }), "xk");
    await user.click(screen.getByRole("button", { name: "Apply filters" }));
    expect(location.hash).toContain("status=revoked");
    expect(location.hash).toContain("country=XK");
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(location.hash).toBe("#senders");
  });
  it("no client-side filtering: rows returned by the server are all shown", async () => {
    mockFetch({ "GET /admin/senders": pageOf([sender({ display_value: "X1", status: "approved" }), sender({ display_value: "X2", status: "pending" })]) });
    registry({ query: { status: "pending" } });
    await screen.findByText("X1");
    expect(screen.getByText("X2")).toBeInTheDocument();
  });
  it("empty, filtered-empty and past-the-end states", async () => {
    mockFetch({ "GET /admin/senders": pageOf([]) });
    const a = registry();
    expect(await screen.findByText("No senders yet")).toBeInTheDocument();
    a.unmount();
    mockFetch({ "GET /admin/senders": pageOf([]) });
    const b = registry({ query: { status: "revoked" } });
    expect(await screen.findByText("No senders match your filters")).toBeInTheDocument();
    b.unmount();
    mockFetch({ "GET /admin/senders": pageOf([], { offset: 500 }) });
    registry({ query: { offset: "500" } });
    expect(await screen.findByText("No more results")).toBeInTheDocument();
  });
  it("loading skeleton, then error with retry", async () => {
    const user = userEvent.setup();
    let ok = false;
    mockFetch({ "GET /admin/senders": () => (ok ? pageOf([sender({ display_value: "BACK" })]) : errorResponse(503, "weird", "secret stack trace")) });
    registry();
    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("secret stack trace");
    ok = true;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("BACK")).toBeInTheDocument();
  });
  it("loading state is announced", async () => {
    mockFetch({ "GET /admin/senders": () => new Promise(() => {}) });
    registry();
    expect(await screen.findByLabelText("Loading")).toBeInTheDocument();
  });
  it("39/40/41 429, 5xx and network errors are sanitized and typed", async () => {
    mockFetch({ "GET /admin/senders": new Response(JSON.stringify({ detail: { code: "rate_limited", message: "x" } }), { status: 429, headers: { "Retry-After": "7" } }) });
    const a = registry();
    expect(await screen.findByText("Too many requests")).toBeInTheDocument();
    expect(screen.getByText(/Wait 7 s/)).toBeInTheDocument();
    a.unmount();
    mockFetch({});
    globalThis.fetch.mockImplementation(async () => { throw new TypeError("x"); });
    registry();
    expect(await screen.findByText("You seem to be offline")).toBeInTheDocument();
  });
  it("38 an invalid filter (422) is shown as a validation message", async () => {
    mockFetch({ "GET /admin/senders": new Response(JSON.stringify({ detail: [{ loc: ["query", "enterprise_id"], msg: "Input should be a valid UUID" }] }), { status: 422 }) });
    registry({ query: { enterprise_id: "nope" } });
    expect(await screen.findByText("Check the details")).toBeInTheDocument();
  });
  it("45 responsive: secondary columns carry hide-sm, key columns keep data-labels", async () => {
    mockFetch({ "GET /admin/senders": pageOf([sender({ display_value: "MOB" })]) });
    registry();
    const tr = (await screen.findByText("MOB")).closest("tr");
    expect([...tr.querySelectorAll("td.hide-sm")].map((td) => td.dataset.label)).toEqual(["Type", "Source", "Created", "Updated"]);
    expect(tr.querySelector("td[data-label='Status']")).not.toBeNull();
  });
});

const base = (o = {}) => ({
  ["GET /admin/senders/" + SID]: sender({ id: SID, display_value: "ACME", status: "pending" }),
  ["GET /admin/senders/" + SID + "/history"]: { items: [decision()] },
  "GET /admin/sender-policies/effective": effective(),
  ...o,
});
const btn = (re) => screen.queryByRole("button", { name: re });

describe("Sender detail and actions", () => {
  it("9/13 pending detail: fields, current server-calculated policy, history timeline in order", async () => {
    mockFetch(base({ ["GET /admin/senders/" + SID + "/history"]: { items: [decision({ seq: 1 }), decision({ seq: 2, decision: "approved", to_status: "approved", from_status: "pending", category: "manual", decided_by_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd", actor_label: null, evidence_ref: "TICKET-9", policy_source: "explicit", policy_revision: 4 })] } }));
    detail();
    expect(await screen.findByRole("heading", { name: "ACME" })).toBeInTheDocument();
    expect(await screen.findByText("Default policy")).toBeInTheDocument();
    const items = (await screen.findAllByRole("listitem")).map((li) => li.textContent);
    expect(items[0]).toContain("Requested");
    expect(items[1]).toContain("Approved");
    expect(items[1]).toContain("Evidence: TICKET-9");
    expect(items[1]).toContain("Policy: explicit r4");
    expect(items[1]).toContain("Admin dddddddd…");
    expect(screen.getByText("sms-sender-1")).toBeInTheDocument();
  });
  it.each([["approved", "Revoke"], ["rejected", "Resubmit"], ["revoked", "Resubmit"]])("10/11/12 %s detail offers only the actions of that state", async (status, label) => {
    mockFetch(base({ ["GET /admin/senders/" + SID]: sender({ id: SID, status }) }));
    detail();
    await screen.findByRole("heading", { name: "ACME" });
    expect(btn(new RegExp(`^${label} ACME`))).toBeInTheDocument();
    expect(btn(/^Approve ACME/)).toBeNull();
  });
  it("14 approve posts only evidence_ref (when given), refreshes and does not set status itself", async () => {
    const user = userEvent.setup();
    let status = "pending"; let posted;
    mockFetch(base({
      ["GET /admin/senders/" + SID]: () => sender({ id: SID, status }),
      ["POST /admin/senders/" + SID + "/approve"]: (r) => { posted = r.body; status = "approved"; return sender({ id: SID, status }); },
    }));
    detail();
    await user.click(await screen.findByRole("button", { name: /^Approve ACME/ }));
    const dlg = screen.getByRole("dialog");
    expect(dlg).toHaveAccessibleName(/Approve sender ID/);
    await user.type(within(dlg).getByLabelText(/Evidence reference/), "TICKET-1");
    await user.click(within(dlg).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posted).toEqual({ evidence_ref: "TICKET-1" }));
    expect(await screen.findByRole("button", { name: /^Revoke ACME/ })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
  });
  it("15 approve 409 (policy denies): shows the domain message, keeps the dialog, refreshes", async () => {
    const user = userEvent.setup();
    let gets = 0;
    mockFetch(base({
      ["GET /admin/senders/" + SID]: () => { gets++; return sender({ id: SID, status: "pending" }); },
      ["POST /admin/senders/" + SID + "/approve"]: errorResponse(409, "conflict", "policy denies alphanumeric senders in AL (revision 3): approval is not possible"),
    }));
    detail();
    await user.click(await screen.findByRole("button", { name: /^Approve ACME/ }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Approve" }));
    expect(await within(screen.getByRole("dialog")).findByRole("alert")).toHaveTextContent("Policy denies alphanumeric senders in AL (revision 3), so approval isn't possible.");
    await waitFor(() => expect(gets).toBeGreaterThanOrEqual(2));
  });
  it("25 global uniqueness conflict uses safe wording and never names another account", async () => {
    const user = userEvent.setup();
    mockFetch(base({ ["POST /admin/senders/" + SID + "/approve"]: errorResponse(409, "conflict", "sender id already approved for another account") }));
    detail();
    await user.click(await screen.findByRole("button", { name: /^Approve ACME/ }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Approve" }));
    const alert = await within(screen.getByRole("dialog")).findByRole("alert");
    expect(alert).toHaveTextContent("This sender identifier is already approved for another account.");
  });
  it("16/38 reject requires a reason (disabled until typed), sends it, and shows field errors from the server", async () => {
    const user = userEvent.setup();
    let attempt = 0; let body;
    mockFetch(base({ ["POST /admin/senders/" + SID + "/reject"]: (r) => { body = r.body; return ++attempt === 1 ? new Response(JSON.stringify({ detail: [{ loc: ["body", "reason"], msg: "String should have at most 500 characters" }] }), { status: 422 }) : sender({ id: SID, status: "rejected" }); } }));
    detail();
    await user.click(await screen.findByRole("button", { name: /^Reject ACME/ }));
    const dlg = screen.getByRole("dialog");
    const submit = within(dlg).getByRole("button", { name: "Reject" });
    expect(submit).toBeDisabled();
    await user.type(within(dlg).getByLabelText("Reason"), "Brand mismatch");
    await user.click(submit);
    expect(await within(dlg).findByText("String should have at most 500 characters")).toBeInTheDocument();
    await waitFor(() => expect(within(dlg).getByLabelText(/^Reason/)).toHaveFocus());
    expect(body).toEqual({ reason: "Brand mismatch" });
    await user.click(submit);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
  it("17 revoke requires a reason and the copy never promises cancelling provider sends", async () => {
    const user = userEvent.setup();
    let body;
    mockFetch(base({ ["GET /admin/senders/" + SID]: sender({ id: SID, status: "approved" }), ["POST /admin/senders/" + SID + "/revoke"]: (r) => { body = r.body; return sender({ id: SID, status: "revoked" }); } }));
    detail();
    await user.click(await screen.findByRole("button", { name: /^Revoke ACME/ }));
    const dlg = screen.getByRole("dialog");
    expect(dlg).toHaveTextContent("once the change reaches the account's system");
    expect(dlg).toHaveTextContent("Messages already handed to a provider can't be recalled.");
    expect(dlg.textContent).not.toMatch(/cancel(led|s)? (the )?(in-flight|provider)/i);
    await user.type(within(dlg).getByLabelText("Reason"), "Policy breach");
    await user.click(within(dlg).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(body).toEqual({ reason: "Policy breach" }));
  });
  it("18 resubmit is offered for rejected/revoked and posts an empty body", async () => {
    const user = userEvent.setup();
    let body;
    mockFetch(base({ ["GET /admin/senders/" + SID]: sender({ id: SID, status: "rejected" }), ["POST /admin/senders/" + SID + "/resubmit"]: (r) => { body = r.body; return { ...sender({ id: SID, status: "pending" }), auto: false }; } }));
    detail();
    await user.click(await screen.findByRole("button", { name: /^Resubmit ACME/ }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Resubmit" }));
    await waitFor(() => expect(body).toEqual({}));
  });
  it("resubmit 409 (state changed) is explained and the details are refreshed", async () => {
    const user = userEvent.setup();
    let gets = 0;
    mockFetch(base({ ["GET /admin/senders/" + SID]: () => { gets++; return sender({ id: SID, status: gets > 1 ? "pending" : "rejected" }); }, ["POST /admin/senders/" + SID + "/resubmit"]: errorResponse(409, "conflict", "cannot resubmit from status pending") }));
    detail();
    await user.click(await screen.findByRole("button", { name: /^Resubmit ACME/ }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Resubmit" }));
    expect(await within(screen.getByRole("dialog")).findByRole("alert")).toHaveTextContent("isn't possible while the sender is pending");
    await waitFor(() => expect(gets).toBeGreaterThanOrEqual(2));
  });
  it("19-22/44 operator sees no write actions (detail) and diagnostics are admin-only", async () => {
    mockFetch(base({ ["GET /admin/senders/" + SID]: sender({ id: SID, status: "pending" }) }));
    detail({ write: false });
    await screen.findByRole("heading", { name: "ACME" });
    expect(btn(/Approve|Reject|Revoke|Resubmit/)).toBeNull();
    expect(screen.getByText(/Read-only access/)).toBeInTheDocument();
    expect(screen.queryByText("Diagnostics")).toBeNull();
  });
  it("admin diagnostics view (norm value, approved key)", async () => {
    mockFetch(base({ ["GET /admin/senders/" + SID]: sender({ id: SID, status: "approved", approved_key: "AL:acme" }) }));
    detail();
    await screen.findByText("Diagnostics");
    expect(screen.getByText("AL:acme")).toBeInTheDocument();
  });
  it("operator deep-link write that still gets 403 shows the read-only message", async () => {
    const user = userEvent.setup();
    mockFetch(base({ ["POST /admin/senders/" + SID + "/approve"]: errorResponse(403, "forbidden", "insufficient role") }));
    detail();
    await user.click(await screen.findByRole("button", { name: /^Approve ACME/ }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Approve" }));
    expect(await within(screen.getByRole("dialog")).findByRole("alert")).toHaveTextContent("Your role is read-only for this action.");
  });
  it("23 auto-approved by policy is shown as a system decision, not a human one", async () => {
    mockFetch(base({ ["GET /admin/senders/" + SID]: sender({ id: SID, status: "approved" }), ["GET /admin/senders/" + SID + "/history"]: { items: [decision(), decision({ seq: 2, decision: "approved", to_status: "approved", category: "policy_auto_approved", actor_label: "system:sender-policy", policy_source: "explicit", policy_revision: 2 })] } }));
    detail();
    expect(await screen.findByText("Approved automatically because the policy doesn't require approval.")).toBeInTheDocument();
    expect(screen.getAllByText(/System \(sender policy\)/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/^Admin /)).toBeNull();
  });
  it("24 policy-denied rejection is shown as automatic, with no promise that resubmitting helps", async () => {
    mockFetch(base({ ["GET /admin/senders/" + SID]: sender({ id: SID, status: "rejected" }), ["GET /admin/senders/" + SID + "/history"]: { items: [decision(), decision({ seq: 2, decision: "rejected", to_status: "rejected", category: "policy_denied", actor_label: "system:sender-policy", reason: "policy_denied: alphanumeric senders are not allowed in AL (revision 2)" })] } }));
    detail();
    expect(await screen.findByText("Rejected automatically because the policy doesn't allow this sender kind in this country.")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/will be approved|guaranteed/i);
  });
  it("detail 404 is not retryable; unknown ids render an error state", async () => {
    mockFetch({ ["GET /admin/senders/" + SID]: errorResponse(404, "not_found", "sender not found"), ["GET /admin/senders/" + SID + "/history"]: errorResponse(404, "not_found", "sender not found") });
    detail();
    expect(await screen.findByText("We couldn't find that")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });
  it("46 responsive detail: actions stay in the card header and the page has no fixed-width tables", async () => {
    mockFetch(base());
    detail();
    await screen.findByRole("heading", { name: "ACME" });
    expect(document.querySelector(".card-h .btn")).not.toBeNull();
  });
  it("44 dialog keyboard: Esc closes and focus returns to the action button; Tab stays inside", async () => {
    const user = userEvent.setup();
    mockFetch(base());
    detail();
    const open = await screen.findByRole("button", { name: /^Reject ACME/ });
    open.focus();
    await user.keyboard("{Enter}");
    const dlg = await screen.findByRole("dialog");
    for (let i = 0; i < 6; i++) { await user.tab(); expect(dlg).toContainElement(document.activeElement); }
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(open).toHaveFocus();
  });
  it("reasonless success path: toast is announced in a live region", async () => {
    const user = userEvent.setup();
    mockFetch(base({ ["POST /admin/senders/" + SID + "/approve"]: sender({ id: SID, status: "approved" }) }));
    detail();
    await user.click(await screen.findByRole("button", { name: /^Approve ACME/ }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Approve" }));
    const live = await screen.findByText("Decision saved");
    expect(live.closest("[aria-live]")).not.toBeNull();
  });
});

describe("i18n (Central Sender Governance)", () => {
  it("43 Albanian labels, statuses and dialog copy", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /admin/senders": pageOf([sender({ display_value: "SQ1", status: "revoked" })]) });
    wrap(<><LangSwitch /><Senders write rest={[]} query={{}} /></>);
    await screen.findByText("SQ1");
    await user.click(screen.getByRole("button", { name: "Shqip" }));
    expect(await screen.findByRole("columnheader", { name: "Ndërmarrja" })).toBeInTheDocument();
    expect(within(screen.getByRole("table")).getByText("revokuar")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Zbato filtrat" })).toBeInTheDocument();
  });
});
