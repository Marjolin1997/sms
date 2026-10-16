import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "../i18n.jsx";
import Campaigns, { emptySchedule, scheduleResult } from "../pages/Campaigns.jsx";
import Contacts from "../pages/Contacts.jsx";
import Messages from "../pages/Messages.jsx";
import { UiProvider } from "../ui.jsx";
import { errorResponse, mockFetch } from "../test/helpers.js";
import { failureInfo, instantFrom } from "../messaging/format.js";

const wrap = (ui) => render(<I18nProvider><UiProvider>{ui}</UiProvider></I18nProvider>);
const at = (hash, ui, routes) => { location.hash = hash; const f = mockFetch(routes); wrap(ui); return f; };
const calls = (f, path) => f.calls.filter((c) => c.path === path);
const qs = (c) => new URLSearchParams(c.url.split("?")[1] || "");
afterEach(() => { location.hash = ""; });

const camp = (o = {}) => ({ id: 12, name: "Autumn sale", channel: "sms", status: "completed", list_id: 3, sender: "ACME", category: "marketing", template_id: null, max_cost: null, rate_per_minute: 300,
  scheduled_at: null, started_at: "2026-10-01T10:00:00Z", completed_at: "2026-10-01T10:30:00Z", pause_reason: null, recipients_total: 120, recipient_counts: { queued: 100, skipped: 20, pending: 0 },
  message_counts: { delivered: 90, failed: 8, sending: 2 }, ...o });
const detail = (o = {}) => ({ ...camp(), created_at: "2026-09-30T08:00:00Z", text: "Big sale today!", subject: null, from_email: null, list_name: "VIP", window_start_hour: null, window_end_hour: null, utc_offset_minutes: 0,
  stats: { recipients: { queued: 100, skipped: 20 }, skipped_reasons: { no_consent: 20 }, messages: { delivered: 90, failed: 8, sending: 2 }, delivery_rate: 0.9, cost: { delivered: "4.5", in_flight: "0.1", refunded: "0.4" } }, ...o });
const msg = (o = {}) => ({ id: "m-1", to: "355691230001", sender: "ACME", status: "delivered", category: "marketing", segments: 1, total_price: "0.05", currency: "EUR", error_code: null, created_at: "2026-10-01T10:05:00Z", ...o });
const contact = (o = {}) => ({ id: 1, phone: "355691230001", email: null, first_name: "Ana", last_name: "Hoxha", external_id: null, attributes: null, created_at: "2026-09-01T00:00:00Z", ...o });
const lst = (o = {}) => ({ id: 3, name: "VIP", created_at: "2026-09-01T00:00:00Z", member_count: 4200, ...o });
const base = (extra = {}) => ({
  "GET /v1/campaigns": [camp(), camp({ id: 11, name: "Draft one", status: "draft", started_at: null, message_counts: {}, recipient_counts: {}, recipients_total: 0 })],
  "GET /v1/lists": [lst(), lst({ id: 4, name: "Empty", member_count: 0 })], "GET /v1/contacts": [contact()],
  "GET /v1/sender-ids": [{ id: 1, value: "ACME", status: "approved", country: "AL" }, { id: 2, value: "BETA", status: "approved", country: "AL" }],
  "GET /v1/templates": [{ id: 7, name: "Welcome", status: "approved" }], "GET /v1/email/domains": [], "GET /v1/wallets": [{ id: 1, currency: "EUR", available: "12.340000", held: "0" }],
  "GET /v1/lists/3/audience": { eligible: 90, excluded: { no_consent: 10 } },
  "GET /v1/campaigns/12": detail(), "GET /v1/campaigns/12/estimate": { recipients: 100, excluded: 20, segments: 100, total: "5.0", currency: "EUR" },
  "GET /v1/campaigns/12/recipients": [{ id: 1, contact_id: 1, status: "queued", reason: null, message_id: 9, address: "355691230001", name: "Ana Hoxha", message_public_id: "m-1", message_status: "delivered", error_code: null },
    { id: 2, contact_id: 2, status: "skipped", reason: "no_consent", message_id: null, address: "355691230002", name: "", message_public_id: null, message_status: null, error_code: null }],
  "GET /v1/campaigns/12/activity": [{ action: "campaign.schedule", at: "2026-10-01T10:00:00Z", by: "you", detail: { at: "2026-10-01T10:00:00+00:00" } }, { action: "campaign.create", at: "2026-09-30T08:00:00Z", by: "you", detail: {} }],
  "GET /v1/messages": { items: [msg(), msg({ id: "m-2", to: "355691230002", status: "failed", error_code: "twilio_30003" })], next_before_id: null }, "GET /v1/messages/m-2/events": [{ from: "queued", to: "failed", detail: "twilio_30003" }],
  ...extra,
});

describe("contacts", () => {
  it("1/2/3 lists people from the server, searches and filters by list on the server", async () => {
    const f = at("#contacts", <Contacts />, base());
    expect(await screen.findByText("Ana Hoxha")).toBeInTheDocument();
    expect(screen.getByText("+355691230001")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Search people"), "ana");
    await waitFor(() => expect(calls(f, "/v1/contacts").some((c) => qs(c).get("q") === "ana")).toBe(true), { timeout: 3000 });
    await waitFor(() => expect(location.hash).toContain("q=ana"));
  });
  it("3b list filter is sent as list_id and kept in the URL", async () => {
    const f = at("#contacts", <Contacts />, base());
    await screen.findByText("Ana Hoxha");
    await userEvent.selectOptions(screen.getByLabelText("Filter by list"), "3");
    await waitFor(() => expect(calls(f, "/v1/contacts").some((c) => qs(c).get("list_id") === "3")).toBe(true));
    expect(location.hash).toContain("list=3");
  });
  it("4 pagination is keyset: load more sends after_id of the last row", async () => {
    const page = Array.from({ length: 51 }, (_, i) => contact({ id: i + 1, first_name: `P${i + 1}`, phone: `3556912300${String(i).padStart(2, "0")}` }));
    const f = at("#contacts", <Contacts />, base({ "GET /v1/contacts": (c) => (qs(c).get("after_id") ? [contact({ id: 99, first_name: "Last" })] : page) }));
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByText(/Last Hoxha/)).toBeInTheDocument();
    expect(calls(f, "/v1/contacts").some((c) => qs(c).get("after_id") === "50")).toBe(true);
  });
  it("5/7 creating sends only harmless formatting changes and shows server errors", async () => {
    const f = at("#contacts?tab=add", <Contacts />, base({ "POST /v1/contacts": errorResponse(422, "invalid_contact", "phone must be E.164") }));
    await userEvent.type(await screen.findByLabelText(/^Phone/), "+355 69-123 4567");
    await userEvent.click(screen.getByRole("button", { name: "Save person" }));
    expect(await screen.findByText("Check the contact details and try again.")).toBeInTheDocument();
    expect(f.calls.find((c) => c.method === "POST" && c.path === "/v1/contacts").body.phone).toBe("+3556912345 67".replace(" ", ""));
  });
  it("6 there is no duplicated phone validation: an odd number is sent as typed and the server decides", async () => {
    const f = at("#contacts?tab=add", <Contacts />, base({ "POST /v1/contacts": contact() }));
    await userEvent.type(await screen.findByLabelText(/^Phone/), "12");
    expect(screen.getByRole("button", { name: "Save person" })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "Save person" }));
    await waitFor(() => expect(f.calls.some((c) => c.method === "POST" && c.body.phone === "12")).toBe(true));
  });
  it("8 empty and filtered-empty states", async () => {
    at("#contacts", <Contacts />, base({ "GET /v1/contacts": [] }));
    expect(await screen.findByText("No contacts yet")).toBeInTheDocument();
  });
  it("9 a cross-tenant contact action returns a safe 404 message", async () => {
    at("#contacts", <Contacts />, base({ "GET /v1/contacts/1/export": errorResponse(404, "not_found", "contact not found") }));
    await userEvent.click(await screen.findByRole("button", { name: "Export data" }));
    expect(await screen.findByText("We couldn't find that. It may have been removed.")).toBeInTheDocument();
  });
});

describe("import", () => {
  const rows = (n) => "phone,first_name\n" + Array.from({ length: n }, (_, i) => `+3556912${String(i).padStart(5, "0")},N${i}`).join("\n");
  const paste = async (text) => { const ta = await screen.findByLabelText("Or paste here"); await userEvent.click(ta); await userEvent.paste(text); };
  it("10/12 sends batches of 1000 and reports success with exact server counts", async () => {
    const f = at("#contacts?tab=import", <Contacts />, base({ "POST /v1/contacts/import": (c) => ({ created: c.body.contacts.length, updated: 0, errors: [] }) }));
    await paste(rows(1500));
    expect(await screen.findByText(/Sent in 2 batches/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Import 1500 people/ }));
    expect(await screen.findByText(/All rows were imported/)).toBeInTheDocument();
    expect(calls(f, "/v1/contacts/import").map((c) => c.body.contacts.length)).toEqual([1000, 500]);
    expect(screen.getByText(/All rows were imported/).closest(".alert")).toHaveTextContent("1500");
  });
  it("11 shows the processing state while a batch is running", async () => {
    let release;
    const gate = new Promise((r) => { release = r; });
    at("#contacts?tab=import", <Contacts />, base({ "POST /v1/contacts/import": async () => { await gate; return { created: 2, updated: 0, errors: [] }; } }));
    await paste(rows(2));
    await userEvent.click(screen.getByRole("button", { name: /Import 2 people/ }));
    expect(await screen.findByText(/Importing… batch 1 of 1/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Import 2 people/ })).toBeDisabled();
    release();
    await screen.findByText(/All rows were imported/);
  });
  it("13/15 partial success lists a bounded number of server errors, never the raw CSV", async () => {
    const errors = Array.from({ length: 60 }, (_, i) => ({ row: i, message: "phone must be E.164" }));
    at("#contacts?tab=import", <Contacts />, base({ "POST /v1/contacts/import": { created: 10, updated: 0, errors } }));
    await paste(rows(70));
    await userEvent.click(screen.getByRole("button", { name: /Import 70 people/ }));
    expect(await screen.findByText(/Partly imported/)).toBeInTheDocument();
    expect(screen.getByText(/first 50 shown/)).toBeInTheDocument();
    expect(document.querySelectorAll("details li")).toHaveLength(50);
    expect(document.querySelector("details").textContent).not.toContain("+3556912");
  });
  it("14 a failing request stops and says how many were imported before it", async () => {
    let n = 0;
    at("#contacts?tab=import", <Contacts />, base({ "POST /v1/contacts/import": () => (++n === 1 ? { created: 1000, updated: 0, errors: [] } : errorResponse(500, "internal", "Traceback boom")) }));
    await paste(rows(1200));
    await userEvent.click(screen.getByRole("button", { name: /Import 1200 people/ }));
    expect(await screen.findByText(/The import stopped/)).toBeInTheDocument();
    expect(screen.getByText(/1000 people were imported before it stopped/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Traceback/);
  });
});

describe("lists", () => {
  it("16/18/20 counts come from the server (4200) even though only a few people are loaded", async () => {
    at("#contacts?tab=lists", <Contacts />, base());
    const row = (await screen.findByText("VIP")).closest("tr");
    expect(row).toHaveTextContent("4200");
  });
  it("17 list detail loads the list's people by list_id and the server audience counts", async () => {
    const f = at("#contacts?tab=lists&list_detail=3", <Contacts />, base());
    expect(await screen.findByText("4200 people in this list")).toBeInTheDocument();
    expect(await screen.findByText(/can receive SMS promotions/)).toBeInTheDocument();
    await waitFor(() => expect(calls(f, "/v1/contacts").some((c) => qs(c).get("list_id") === "3")).toBe(true));
  });
  it("19 there is no persisted segment feature: only lists are offered", async () => {
    at("#contacts", <Contacts />, base());
    await screen.findByText("Ana Hoxha");
    expect(screen.queryByText(/segment/i)).toBeNull();
  });
});

describe("campaign list", () => {
  it("21/22 shows server counts and statuses and filters on the server (URL kept)", async () => {
    const f = at("#campaigns", <Campaigns />, base());
    const row = (await screen.findByText("Autumn sale")).closest("tr");
    expect(row).toHaveTextContent("120");
    expect(row).toHaveTextContent("90");
    expect(within(row).getByText("completed")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Filter by status"), "running");
    await waitFor(() => expect(calls(f, "/v1/campaigns").some((c) => qs(c).get("status") === "running")).toBe(true));
    expect(location.hash).toContain("status=running");
  });
  it("22b search is debounced and sent as q", async () => {
    const f = at("#campaigns", <Campaigns />, base());
    await screen.findByText("Autumn sale");
    await userEvent.type(screen.getByLabelText("Search campaigns"), "aut");
    await waitFor(() => expect(calls(f, "/v1/campaigns").some((c) => qs(c).get("q") === "aut")).toBe(true), { timeout: 3000 });
  });
  it("23 pagination is keyset (before_id) and the page size is 25", async () => {
    const many = Array.from({ length: 26 }, (_, i) => camp({ id: 100 - i, name: `C${i}` }));
    const f = at("#campaigns", <Campaigns />, base({ "GET /v1/campaigns": (c) => (qs(c).get("before_id") ? [camp({ id: 5, name: "Oldest" })] : many) }));
    await screen.findByText("C0");
    expect(screen.queryByText("C25")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Older" }));
    expect(await screen.findByText("Oldest")).toBeInTheDocument();
    expect(calls(f, "/v1/campaigns").some((c) => qs(c).get("before_id") === "76")).toBe(true);
  });
  it("24-28 every backend status renders; an unknown one is neutral and does not crash", async () => {
    const sts = ["draft", "scheduled", "preparing", "running", "paused", "completed", "cancelled", "weird_state"];
    at("#campaigns", <Campaigns />, base({ "GET /v1/campaigns": sts.map((s, i) => camp({ id: i + 1, name: `n-${s}`, status: s })) }));
    await screen.findByText("n-draft");
    for (const s of sts) expect(screen.getByText(`n-${s}`).closest("tr")).toHaveTextContent(s.replace("_", " "));
    expect(screen.getByText("weird state").closest(".badge").className).toMatch(/muted/);
  });
  it("29 filtered-empty offers to clear the filters", async () => {
    at("#campaigns?status=paused", <Campaigns />, base({ "GET /v1/campaigns": [] }));
    expect(await screen.findByText("No campaigns match")).toBeInTheDocument();
  });
});

describe("campaign creation", () => {
  const next = () => userEvent.click(screen.getByRole("button", { name: "Next" }));
  const fillTo = async (step) => {
    await userEvent.type(await screen.findByLabelText(/^Name/), "Winter promo");
    await next();
    if (step === 1) return;
    await userEvent.selectOptions(await screen.findByLabelText(/^Who receives it/), "3");
    await screen.findByText(/can be contacted/);
    await next();
    if (step === 2) return;
    await userEvent.type(await screen.findByRole("textbox", { name: /Message/ }), "Hello world");
    await next();
    if (step === 3) return;
    await next();
  };
  it("30/31 sender choices are only the server's approved sender IDs", async () => {
    at("#campaigns/new", <Campaigns />, base());
    const sel = await screen.findByLabelText(/^From/);
    await waitFor(() => expect(within(sel).getAllByRole("option").map((o) => o.textContent)).toEqual(["ACME", "BETA"]));
  });
  it("32 the audience preview comes from the server and never from a client page", async () => {
    const f = at("#campaigns/new", <Campaigns />, base());
    await userEvent.type(await screen.findByLabelText(/^Name/), "Winter promo");
    await next();
    await userEvent.selectOptions(await screen.findByLabelText(/^Who receives it/), "3");
    expect(await screen.findByText("90")).toBeInTheDocument();
    expect(screen.getByText(/10 · No recorded consent/)).toBeInTheDocument();
    expect(calls(f, "/v1/lists/3/audience").length).toBeGreaterThan(0);
  });
  it("33/34 composer labels segment counts as an estimate and templates are selectable", async () => {
    at("#campaigns/new", <Campaigns />, base());
    await fillTo(2);
    await userEvent.type(await screen.findByRole("textbox", { name: /Message/ }), "x".repeat(170));
    expect(screen.getByText(/estimate; the server counts the final number/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Message source"), "template");
    expect(await screen.findByRole("option", { name: "Welcome" })).toBeInTheDocument();
  });
  it("35/36/37 schedule: now or later, explicit time zone, exact instant, past rejected", async () => {
    at("#campaigns/new", <Campaigns />, base());
    await fillTo(3);
    await userEvent.click(screen.getByLabelText("Schedule for later"));
    await userEvent.selectOptions(screen.getByLabelText("Time zone"), "utc");
    const d = new Date(Date.now() + 2 * 86400000).toISOString();
    fireEvent.change(screen.getByLabelText("Date"), { target: { value: d.slice(0, 10) } });
    fireEvent.change(screen.getByLabelText("Time"), { target: { value: "09:30" } });
    expect(await screen.findByText(new RegExp(`${d.slice(0, 10)} 09:30 UTC`))).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Date"), { target: { value: "2020-01-01" } });
    expect(await screen.findByText(/already passed/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
  });
  it("scheduleResult/instantFrom do not shift times silently", () => {
    expect(instantFrom("2030-06-01", "09:30", "utc").toISOString()).toBe("2030-06-01T09:30:00.000Z");
    expect(instantFrom("garbage", "09:30", "utc")).toBeNull();
    const r = scheduleResult({ ...emptySchedule(), mode: "later", date: "2030-06-01", time: "09:30", zone: "utc" }, Date.parse("2030-01-01"));
    expect(r.ok && r.iso).toBe("2030-06-01T09:30:00.000Z");
    expect(scheduleResult({ ...emptySchedule(), mode: "later", date: "2020-01-01", time: "09:30", zone: "utc" }).ok).toBe(false);
  });
  it("38 review shows name, sender, recipients, message, schedule and the server balance — no invented cost", async () => {
    at("#campaigns/new", <Campaigns />, base());
    await fillTo(4);
    const review = await screen.findByLabelText("Review", { selector: "dl" });
    expect(review).toHaveTextContent("Winter promo");
    expect(review).toHaveTextContent("ACME");
    expect(review).toHaveTextContent("VIP");
    expect(review).toHaveTextContent("Hello world");
    expect(review).toHaveTextContent(/12[.,]34 EUR/);
    expect(screen.getByText(/cost estimate appears on the campaign page/)).toBeInTheDocument();
  });
  it("39 duplicate submit is prevented: one create call even with two clicks", async () => {
    let release;
    const gate = new Promise((r) => { release = r; });
    const f = at("#campaigns/new", <Campaigns />, base({ "POST /v1/campaigns": async () => { await gate; return camp({ id: 77 }); }, "POST /v1/campaigns/77/schedule": camp({ id: 77 }) }));
    await fillTo(4);
    const send = screen.getByRole("button", { name: "Save draft" });
    await userEvent.click(send);
    await userEvent.click(send);
    release();
    await waitFor(() => expect(location.hash).toContain("campaigns/77"));
    expect(f.calls.filter((c) => c.method === "POST" && c.path === "/v1/campaigns")).toHaveLength(1);
  });
  it("40/41 422 and 409 from the server are shown in plain language", async () => {
    at("#campaigns/new", <Campaigns />, base({ "POST /v1/campaigns": errorResponse(409, "conflict", "campaign name already exists") }));
    await fillTo(4);
    await userEvent.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByText("This conflicts with what already exists. Refresh and try again.")).toBeInTheDocument();
  });
  it("42 insufficient balance is the server's verdict with a top-up link — nothing computed locally", async () => {
    at("#campaigns/new", <Campaigns />, base({ "POST /v1/campaigns": camp({ id: 78, status: "draft" }), "POST /v1/campaigns/78/schedule": errorResponse(402, "insufficient_funds", "insufficient available funds") }));
    await fillTo(4);
    await userEvent.click(screen.getByRole("button", { name: "Send now" }));
    await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Send now" }));
    expect(await screen.findByText(/Your wallet balance is too low/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Top up" })).toHaveAttribute("href", "#wallet");
    expect(screen.getByRole("link", { name: "Open the draft" }).getAttribute("href")).toBe("#campaigns/78");
  });
  for (const [code, text] of [["sender_revoked", /was revoked/], ["sender_pending", /still waiting for approval/], ["not_synchronized", /not synchronized yet/]]) {
    it(`43-45 sender error ${code} uses the shared translation`, async () => {
      at("#campaigns/12", <Campaigns />, base({ "GET /v1/campaigns/12": detail({ status: "draft" }), "POST /v1/campaigns/12/schedule": errorResponse(422, code, "x") }));
      await userEvent.click(await screen.findByRole("button", { name: "Send now" }));
      await userEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Send now" }));
      expect(await screen.findByText(text)).toBeInTheDocument();
    });
  }
});

describe("campaign detail", () => {
  it("46/50 overview shows server identity, schedule and delivery summary", async () => {
    at("#campaigns/12", <Campaigns />, base());
    expect(await screen.findByText("Autumn sale")).toBeInTheDocument();
    expect(await screen.findByText("Big sale today!")).toBeInTheDocument();
    expect(screen.getByText("VIP")).toBeInTheDocument();
    expect(screen.getByText("Delivery summary")).toBeInTheDocument();
    expect(screen.getByText("90")).toBeInTheDocument();
  });
  it("47/53 recipients tab is server-paginated with after_id and shows contact and delivery state", async () => {
    const page = Array.from({ length: 26 }, (_, i) => ({ id: i + 1, contact_id: i, status: "queued", reason: null, message_id: i, address: `3556912${String(i).padStart(5, "0")}`, name: `R${i}`, message_public_id: `m${i}`, message_status: "delivered", error_code: null }));
    const f = at("#campaigns/12?tab=recipients", <Campaigns />, base({ "GET /v1/campaigns/12/recipients": (c) => (qs(c).get("after_id") ? [{ ...page[0], id: 99, name: "Later" }] : page) }));
    expect(await screen.findByText("R0")).toBeInTheDocument();
    expect(screen.queryByText("R25")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("Later")).toBeInTheDocument();
    expect(calls(f, "/v1/campaigns/12/recipients").some((c) => qs(c).get("after_id") === "25")).toBe(true);
  });
  it("47b skipped recipients show the server's reason", async () => {
    at("#campaigns/12?tab=recipients", <Campaigns />, base());
    expect(await screen.findByText("No recorded consent")).toBeInTheDocument();
  });
  it("48/54/55/56 traffic tab filters by this campaign on the server, opens a timeline and shows failure categories", async () => {
    const f = at("#campaigns/12?tab=traffic", <Campaigns />, base());
    expect(await screen.findByText("+355691230002")).toBeInTheDocument();
    expect(calls(f, "/v1/messages").some((c) => qs(c).get("campaign_id") === "12")).toBe(true);
    const failed = screen.getByText("+355691230002").closest("tr");
    expect(within(failed).getByText("Rejected by the provider")).toBeInTheDocument();
    await userEvent.click(failed);
    expect(await screen.findByText("Reference m-2")).toBeInTheDocument();
    expect(await screen.findByText("twilio_30003", { selector: "td" })).toBeInTheDocument();
  });
  it("57 an unknown provider result is shown as 'sending', never as failed", async () => {
    at("#campaigns/12?tab=traffic", <Campaigns />, base({ "GET /v1/messages": { items: [msg({ id: "m-3", status: "sending", error_code: null })], next_before_id: null } }));
    const row = (await screen.findByText("+355691230001")).closest("tr");
    expect(within(row).getByText("sending").closest(".badge").className).not.toMatch(/\bbad\b/);
    expect(row).not.toHaveTextContent(/failed/i);
  });
  it("49 activity shows only recorded events, with the actor as 'you' (no key ids)", async () => {
    at("#campaigns/12?tab=activity", <Campaigns />, base());
    expect(await screen.findByText("Scheduled / started")).toBeInTheDocument();
    expect(screen.getByText("Campaign created")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/key:/);
  });
  it("49b no activity yet is an honest empty state", async () => {
    at("#campaigns/12?tab=activity", <Campaigns />, base({ "GET /v1/campaigns/12/activity": [] }));
    expect(await screen.findByText("No recorded activity.")).toBeInTheDocument();
  });
  it("51/52 delivery summary is not reconstructed from rows", async () => {
    at("#campaigns/12", <Campaigns />, base({ "GET /v1/messages": { items: [], next_before_id: null } }));
    await screen.findByText("Delivery summary");
    expect(screen.getByText("90")).toBeInTheDocument();
  });
  it("a draft offers Send/Schedule with the server estimate and balance side by side, no verdict", async () => {
    at("#campaigns/12", <Campaigns />, base({ "GET /v1/campaigns/12": detail({ status: "draft" }) }));
    expect(await screen.findByText("Send or schedule")).toBeInTheDocument();
    expect(await screen.findByText(/Estimated cost/)).toBeInTheDocument();
    expect(screen.getByText(/Available balance/)).toBeInTheDocument();
  });
  it("there is no Retry/Resend control (the backend offers none)", async () => {
    at("#campaigns/12", <Campaigns />, base({ "GET /v1/campaigns/12": detail({ status: "completed" }) }));
    await screen.findByText("Delivery summary");
    expect(screen.queryByRole("button", { name: /retry|resend/i })).toBeNull();
  });
  it("running campaigns offer Pause; paused offer Resume; the cancel dialog explains the limits", async () => {
    at("#campaigns/12", <Campaigns />, base({ "GET /v1/campaigns/12": detail({ status: "running" }) }));
    expect(await screen.findByRole("button", { name: "Pause" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByText(/already sent can't be recalled/)).toBeInTheDocument();
  });
});

describe("messages / traffic", () => {
  it("filters are sent to the server and kept in the URL", async () => {
    const f = at("#messages?sender=ACME&campaign=12&from=2026-10-01&to=2026-10-02&status=failed", <Messages />, base());
    await screen.findByText("+355691230001");
    const c = calls(f, "/v1/messages").pop();
    expect(Object.fromEntries(qs(c))).toMatchObject({ sender: "ACME", campaign_id: "12", created_from: "2026-10-01", created_to: "2026-10-02", status: "failed" });
  });
  it("an inverted date range shows an error and is not applied", async () => {
    const f = at("#messages", <Messages />, base());
    await screen.findByText("+355691230001");
    await userEvent.type(screen.getByLabelText("From date"), "2026-10-10");
    await userEvent.type(screen.getByLabelText("To date"), "2026-10-01");
    expect(await screen.findByText(/must not be before the start date/)).toBeInTheDocument();
    expect(calls(f, "/v1/messages").every((c) => !qs(c).get("created_to"))).toBe(true);
    expect(location.hash).not.toContain("to=2026-10-01");
  });
  it("failure categories come from the backend error_code; unknown codes stay visible", () => {
    expect(failureInfo("insufficient_funds").category).toBe("balance");
    expect(failureInfo("sender_revoked").category).toBe("sender");
    expect(failureInfo("twilio_30003").category).toBe("provider");
    expect(failureInfo("zzz")).toMatchObject({ category: "other", code: "zzz" });
    expect(failureInfo(null)).toBeNull();
  });
  it("pagination uses before_id and Older/Newest", async () => {
    const f = at("#messages", <Messages />, base({ "GET /v1/messages": (c) => ({ items: [msg({ id: qs(c).get("before_id") ? "m-old" : "m-new" })], next_before_id: qs(c).get("before_id") ? null : 50 }) }));
    await screen.findByText("+355691230001");
    await userEvent.click(screen.getByRole("button", { name: "Older" }));
    await waitFor(() => expect(calls(f, "/v1/messages").some((c) => qs(c).get("before_id") === "50")).toBe(true));
  });
});

describe("errors, a11y and i18n", () => {
  for (const [status, code, text] of [[403, "forbidden", /Your role doesn't allow this action/], [404, "not_found", /couldn't find that/i], [429, "rate_limited", /sending too fast|Too many/i], [500, "internal", /Something went wrong on our side/]]) {
    it(`${status} on the campaign list is clear and sanitized`, async () => {
      at("#campaigns", <Campaigns />, base({ "GET /v1/campaigns": errorResponse(status, code, "Traceback (most recent call last): secret_provider_payload") }));
      expect((await screen.findAllByText(text)).length).toBeGreaterThan(0);
      expect(document.body.textContent).not.toMatch(/Traceback|secret_provider_payload/);
    });
  }
  it("401 is surfaced as an expired session", async () => {
    at("#campaigns", <Campaigns />, base({ "GET /v1/campaigns": errorResponse(401, "unauthorized", "x") }));
    expect(await screen.findByText(/session is no longer valid/i)).toBeInTheDocument();
  });
  it("network failure offers retry", async () => {
    let fail = true;
    at("#campaigns", <Campaigns />, base({ "GET /v1/campaigns": () => { if (fail) throw new TypeError("Failed to fetch"); return [camp()]; } }));
    expect(await screen.findByText("You seem to be offline")).toBeInTheDocument();
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Autumn sale")).toBeInTheDocument();
  });
  it("keyboard: a campaign row opens with Enter", async () => {
    at("#campaigns", <Campaigns />, base());
    const row = (await screen.findByText("Autumn sale")).closest("tr");
    row.focus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(location.hash).toContain("campaigns/12"));
  });
  it("mobile hooks: wide diagnostic columns are marked hide-sm and cells carry labels", async () => {
    at("#campaigns", <Campaigns />, base());
    const t = (await screen.findByText("Autumn sale")).closest("table");
    expect(t.querySelectorAll(".hide-sm").length).toBeGreaterThan(0);
    expect(t.querySelectorAll("td[data-label]").length).toBeGreaterThan(0);
  });
  it("EN/SQ: new strings are translated", async () => {
    const sq = (await import("../locales/sq/index.js")).default;
    for (const k of ["When to send", "Schedule for later", "Why not delivered", "Importing… batch {a} of {b}. Don't close this page.", "Newest", "Older", "Delivery summary"]) expect(sq[k], k).toBeTruthy();
  });
  it("no business logic in the client: the pages never compute status, price, balance or recipient counts", async () => {
    const src = await Promise.all(["../pages/Campaigns.jsx", "../pages/Contacts.jsx", "../pages/Messages.jsx"].map((p) => import(`${p}?raw`).then((m) => m.default)));
    const all = src.join("\n");
    expect(all).not.toMatch(/\.reduce\(\s*\(\s*\w+,\s*\w+\s*\)\s*=>\s*\w+\s*\+\s*Number/);
    expect(all).not.toMatch(/status\s*=\s*["']running["']/);
  });
});
