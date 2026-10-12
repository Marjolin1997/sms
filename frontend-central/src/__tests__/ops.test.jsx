import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App.jsx";
import { setToken } from "../api.js";
import { I18nProvider, LangSwitch } from "../i18n.jsx";
import OpsAlerts from "../pages/OpsAlerts.jsx";
import OpsBootstrap from "../pages/OpsBootstrap.jsx";
import OpsOverview from "../pages/OpsOverview.jsx";
import OpsSync from "../pages/OpsSync.jsx";
import { ADMIN, OPERATOR, allPass, chk, errorResponse, mockFetch, opsBody, pageOf, withSection } from "../test/helpers.js";
import { UiProvider } from "../ui.jsx";

const URL_ = "GET /admin/operations/sender";
const wrap = (ui) => render(<I18nProvider><UiProvider>{ui}</UiProvider></I18nProvider>);
const boot = async (me, hash, body) => {
  setToken("t"); location.hash = hash;
  mockFetch({ "GET /auth/me": me, [URL_]: body ?? opsBody(), "GET /admin/senders": pageOf([]), "GET /admin/sender-policies": pageOf([]) });
  render(<App />);
  return screen.findByRole("navigation", { name: "Main navigation" });
};
const overall = () => within(screen.getByRole("status", { name: "Overall sender governance health" }));
afterEach(() => vi.useRealTimers());

describe("route, access and navigation", () => {
  it("1/2 admin opens the Operations route and sees the Operations group", async () => {
    const n = await boot(ADMIN, "#ops");
    expect(await screen.findByText("Overall sender governance health")).toBeInTheDocument();
    expect(within(n).getByText("Operations")).toBeInTheDocument();
    for (const l of ["Sender Readiness", "Sync & Requests", "Bootstrap & Cutover", "Alerts"]) expect(within(n).getByRole("link", { name: new RegExp(l) })).toBeInTheDocument();
  });
  it("3 operator can read the same data and gets no write control", async () => {
    await boot(OPERATOR, "#ops");
    await screen.findByText("Overall sender governance health");
    expect(screen.getByText("Read-only access")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve|Reject|Revoke|Resubmit|Save|Apply|Resolve|Enable/i })).toBeNull();
  });
  it("4/32 unauthenticated users see the login; a 401 on the endpoint returns to login", async () => {
    location.hash = "#ops"; mockFetch({});
    const a = render(<App />);
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
    a.unmount();
    setToken("t"); mockFetch({ "GET /auth/me": ADMIN, [URL_]: errorResponse(401, "unauthorized", "x") });
    render(<App />);
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByText("Your session has ended. Please sign in again.")).toBeInTheDocument();
  });
  it("27 Operations pages have no mutation controls and make only GET requests", async () => {
    const f = mockFetch({ [URL_]: opsBody({ alerts: [{ code: "sync_gap", severity: "critical", check: "sync_sequence_consistent", section: "sync", summary: "gap", observed_at: "2026-03-01T10:00:00+00:00" }] }) });
    for (const P of [OpsOverview, OpsSync, OpsBootstrap, OpsAlerts]) {
      const r = wrap(<P />);
      await screen.findByText(/Generated/);
      expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Refresh"]);
      r.unmount();
    }
    expect(f.calls.every((c) => c.method === "GET")).toBe(true);
  });
});

describe("overall status semantics", () => {
  it.each([["pass", "good"], ["warn", "warn"], ["fail", "bad"], ["unknown", "muted"]])("5-8 overall %s renders text + tone %s", async (status, tone) => {
    mockFetch({ [URL_]: status === "pass" ? allPass() : opsBody({ status }) });
    wrap(<OpsOverview />);
    const badge = (await screen.findByRole("status", { name: "Overall sender governance health" })).querySelector(".badge");
    expect(badge).toHaveClass(tone);
    expect(badge.textContent).toContain(status);
    expect(badge.querySelector("[aria-hidden]")).not.toBeNull();
  });
  it("C: an unknown or garbage status is never rendered as pass", async () => {
    mockFetch({ [URL_]: opsBody({ status: "great", sections: [{ code: "central_readiness", status: "ok??", checks: [{ code: "registry_states_valid", status: null, summary: "x", details: {} }] }] }) });
    wrap(<OpsOverview />);
    const o = await screen.findByRole("status", { name: "Overall sender governance health" });
    expect(o.querySelector(".badge")).toHaveClass("muted");
    expect(document.querySelectorAll(".badge.good").length).toBe(0);
  });
  it("unknown overall explains why it cannot be healthy", async () => {
    mockFetch({ [URL_]: opsBody() });
    wrap(<OpsOverview />);
    expect(await screen.findByText(/can't be confirmed as healthy/)).toBeInTheDocument();
  });
});

describe("overview and central readiness", () => {
  it("9 lists the server's central checks with labels and summaries; cards reflect section statuses", async () => {
    mockFetch({ [URL_]: opsBody() });
    wrap(<OpsOverview />);
    expect(await screen.findByText("Registry states are valid")).toBeInTheDocument();
    expect(screen.getByText("12 sender(s)")).toBeInTheDocument();
    const cards = screen.getAllByRole("link").map((a) => a.getAttribute("aria-label"));
    expect(cards).toEqual(expect.arrayContaining(["Central readiness: pass", "Sync feed: unknown", "Request intake: unknown", "Bootstrap: unknown", "Critical drift: unknown", "Cutover evidence: unknown"]));
  });
  it("a failing central check shows FAIL with the recommended operator action", async () => {
    mockFetch({ [URL_]: withSection(opsBody({ status: "fail" }), "central_readiness", { status: "fail", checks: [chk("current_decision_matches_state", "fail", "2 sender(s) whose current decision/status disagree")] }) });
    wrap(<OpsOverview />);
    const row = (await screen.findByText("Current decision matches state")).closest("li");
    expect(within(row).getByText("fail")).toHaveClass("bad");
    expect(row).toHaveTextContent("Recommended action");
    expect(row).toHaveTextContent("2 sender(s) whose current decision/status disagree");
  });
  it("shows when readiness was computed and that it is cached", async () => {
    mockFetch({ [URL_]: opsBody({ readiness_cached: true }) });
    wrap(<OpsOverview />);
    expect(await screen.findByText(/cached up to 30 s/)).toBeInTheDocument();
  });
});

describe("sync and requests", () => {
  it("10 healthy feed: sequence, retained events and check pass", async () => {
    mockFetch({ [URL_]: opsBody() });
    wrap(<OpsSync />);
    expect(await screen.findByText("Feed tail present in the outbox.")).toBeInTheDocument();
    expect(screen.getByText("Latest sequence").nextSibling).toHaveTextContent("7");
    expect(screen.getByText("Retained events").nextSibling).toHaveTextContent("7");
    expect(screen.getByText("ab12cd34")).toBeInTheDocument();
  });
  it("11 a feed gap is FAIL with the recovery action", async () => {
    mockFetch({ [URL_]: withSection(opsBody(), "sync", { status: "fail", checks: [chk("sync_sequence_consistent", "fail", "The latest feed sequence has no outbox event (gap).")] }) });
    wrap(<OpsSync />);
    const row = (await screen.findByText("Sync feed is consistent")).closest("li");
    expect(within(row).getByText("fail")).toBeInTheDocument();
    expect(row).toHaveTextContent("snapshot recovery");
  });
  it("12 Enterprise sync runtime is explicitly unavailable, never healthy", async () => {
    mockFetch({ [URL_]: opsBody() });
    wrap(<OpsSync />);
    const row = (await screen.findByText("Enterprise sync runtime")).closest("li");
    expect(within(row).getByText("unknown")).toHaveClass("muted");
    expect(row).toHaveTextContent("Not connected to Central: evidence unavailable here.");
    expect(row).toHaveTextContent("python -m scripts.sender_sync_readiness");
    expect(row.textContent).not.toMatch(/\bpass\b/);
  });
  it("13/14 request backlog and permanent failures render generically if the backend ever supplies them", async () => {
    mockFetch({ [URL_]: withSection(opsBody(), "requests", { status: "fail", checks: [chk("request_backlog", "warn", "12 requests waiting"), chk("request_permanent_failure", "fail", "3 permanently failed")] }) });
    wrap(<OpsSync />);
    expect((await screen.findByText("request_backlog")).closest("li")).toHaveTextContent("12 requests waiting");
    expect(screen.getByText("request_permanent_failure").closest("li")).toHaveTextContent("3 permanently failed");
  });
  it("request intake metrics and Enterprise outbox unavailable", async () => {
    mockFetch({ [URL_]: opsBody() });
    wrap(<OpsSync />);
    expect(await screen.findByText("Requests received")).toBeInTheDocument();
    expect(screen.getByText("By outcome").nextSibling).toHaveTextContent("created: 3");
    expect(screen.getByText("Enterprise request outbox").closest("li")).toHaveTextContent("evidence unavailable");
  });
});

describe("bootstrap, cutover and shadow", () => {
  it("15 completed run: counts, shortened hashes, source revision", async () => {
    mockFetch({ [URL_]: opsBody() });
    wrap(<OpsBootstrap />);
    expect(await screen.findByText("aaaaaaaaaaaa")).toBeInTheDocument();
    expect(screen.getByText("bbbbbbbbbbbb")).toBeInTheDocument();
    expect(screen.getByText("Senders scanned").nextSibling).toHaveTextContent("10");
    expect(screen.getByText("8c820ff")).toBeInTheDocument();
  });
  it("16 unresolved items warn and say cutover is blocked", async () => {
    const b = opsBody();
    b.metrics.bootstrap.latest.unresolved_count = 3;
    mockFetch({ [URL_]: withSection(b, "bootstrap", { status: "warn", checks: [chk("bootstrap_run"), chk("bootstrap_unresolved", "warn", "The latest run reported 3 unresolved item(s); they block cutover readiness.", { unresolved_count: 3, blocks_cutover: true })] }) });
    wrap(<OpsBootstrap />);
    const row = (await screen.findByText("Bootstrap unresolved items")).closest("li");
    expect(within(row).getByText("warn")).toHaveClass("warn");
    expect(row).toHaveTextContent("block cutover readiness");
    expect(row).toHaveTextContent("unresolved_count: 3");
    expect(row).toHaveTextContent("Recommended action");
  });
  it("17 issue categories live on the Enterprise side: shown as unavailable, not invented", async () => {
    mockFetch({ [URL_]: opsBody() });
    wrap(<OpsBootstrap />);
    const row = (await screen.findByText("Bootstrap issue categories")).closest("li");
    expect(row).toHaveTextContent("evidence unavailable");
    expect(screen.queryByText(/identity_conflict|global_key_conflict/)).toBeNull();
  });
  it("no run recorded", async () => {
    const b = opsBody(); b.metrics.bootstrap = { runs: {}, latest: null };
    mockFetch({ [URL_]: withSection(b, "bootstrap", { status: "unknown", checks: [chk("bootstrap_run", "unknown", "No bootstrap run is recorded in Central.")] }) });
    wrap(<OpsBootstrap />);
    expect((await screen.findAllByText("No bootstrap run is recorded in Central.")).length).toBeGreaterThan(0);
  });
  it("18/19 shadow sample progress and critical drift are unknown (UNKNOWN, never PASS) and carry the tool hint", async () => {
    mockFetch({ [URL_]: opsBody() });
    wrap(<OpsBootstrap />);
    const row = (await screen.findByText("Shadow comparison and drift")).closest("li");
    expect(within(row).getByText("unknown")).toBeInTheDocument();
    expect(row).toHaveTextContent("python -m scripts.sender_authority_readiness");
    expect(screen.getByText("Cutover evidence").closest("li")).toHaveTextContent("python -m scripts.sender_policy_readiness");
    expect(screen.getByText(/No change can be made from here/)).toBeInTheDocument();
  });
});

describe("alerts", () => {
  const alert = (o) => ({ code: "readiness_fail", severity: "critical", check: "registry_states_valid", section: "central_readiness", summary: "1 sender(s) with an invalid approved_key/status", observed_at: "2026-03-01T10:00:00+00:00", ...o });
  it("20 lists alerts with severity, label, summary and timestamp", async () => {
    mockFetch({ [URL_]: opsBody({ status: "fail", alerts: [alert(), alert({ code: "bootstrap_unresolved", severity: "warning", check: "bootstrap_unresolved", section: "bootstrap", summary: "3 unresolved" })] }) });
    wrap(<OpsAlerts />);
    expect(await screen.findByText("Central readiness check failed")).toBeInTheDocument();
    const rows = screen.getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("critical")).toHaveClass("bad");
    expect(within(rows[1]).getByText("warning")).toHaveClass("warn");
    expect(within(rows[0]).getByText(/UTC/)).toBeInTheDocument();
  });
  it("21 empty state, and Enterprise alerts are explicitly unavailable", async () => {
    mockFetch({ [URL_]: opsBody() });
    wrap(<OpsAlerts />);
    expect(await screen.findByText("No active alerts")).toBeInTheDocument();
    expect(screen.getByText("Enterprise sender alerts", { selector: "b" }).closest("li")).toHaveTextContent("evidence unavailable");
  });
  it("28 mobile layout: secondary alert columns are hidden on narrow screens", async () => {
    mockFetch({ [URL_]: opsBody({ alerts: [alert()] }) });
    wrap(<OpsAlerts />);
    const tr = (await screen.findByText("Central readiness check failed")).closest("tr");
    expect([...tr.querySelectorAll("td.hide-sm")].map((td) => td.dataset.label)).toEqual(["Check", "Section"]);
  });
});

describe("refresh, polling and partial data", () => {
  it("23 manual refresh refetches", async () => {
    const user = userEvent.setup();
    const f = mockFetch({ [URL_]: opsBody() });
    wrap(<OpsOverview />);
    await screen.findByText(/Generated/);
    const n = f.calls.length;
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(f.calls.length).toBe(n + 1));
  });
  it("24 polls on an interval and stops after unmount", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const f = mockFetch({ [URL_]: opsBody() });
    const r = wrap(<OpsOverview />);
    await screen.findByText(/Generated/);
    const n = f.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(46000); });
    expect(f.calls.length).toBe(n + 1);
    r.unmount();
    await act(async () => { await vi.advanceTimersByTimeAsync(200000); });
    expect(f.calls.length).toBe(n + 1);
  });
  it("22 a failed refresh keeps the last data and flags it as stale; a partial (unknown) section does not fail the page", async () => {
    const user = userEvent.setup();
    let fail = false;
    mockFetch({ [URL_]: () => (fail ? errorResponse(503, "weird", "boom") : opsBody()) });
    wrap(<OpsOverview />);
    await screen.findByText("Registry states are valid");
    fail = true;
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByText("Showing the last data we received.")).toBeInTheDocument();
    expect(screen.getByText("Registry states are valid")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("boom");
  });
  it("25 timestamps: exact UTC plus relative, same format everywhere", async () => {
    mockFetch({ [URL_]: opsBody() });
    wrap(<OpsSync />);
    await screen.findByText(/Generated/);
    const t = document.querySelector("time[datetime='2026-03-01T10:00:00+00:00']");
    expect(t.textContent).toMatch(/01\/03\/2026, 10:00:00 UTC|03\/01\/2026, 10:00:00 UTC/);
    expect(t.title).toMatch(/UTC$/);
  });
  it("26 details are sanitized: only primitives render, markup is escaped, unknown keys never crash", async () => {
    const b = withSection(opsBody(), "central_readiness", { checks: [chk("registry_states_valid", "pass", "<img src=x onerror=alert(1)>", { count: 3, nested: { a: 1 }, list: [1], ok: true })] });
    mockFetch({ [URL_]: b });
    wrap(<OpsOverview />);
    const li = (await screen.findByText("Registry states are valid")).closest("li");
    expect(li).toHaveTextContent("count: 3 · ok: true");
    expect(li.textContent).not.toMatch(/nested|list/);
    expect(document.querySelector("img[src='x']")).toBeNull();
    expect(li).toHaveTextContent("<img src=x onerror=alert(1)>");
  });
});

describe("errors", () => {
  it("33 403 shows the permission state without leaking", async () => {
    mockFetch({ [URL_]: errorResponse(403, "forbidden", "insufficient role") });
    wrap(<OpsOverview />);
    expect(await screen.findByText("You don't have access")).toBeInTheDocument();
  });
  it("34 429 with Retry-After", async () => {
    mockFetch({ [URL_]: new Response(JSON.stringify({ detail: { code: "rate_limited", message: "x" } }), { status: 429, headers: { "Retry-After": "9" } }) });
    wrap(<OpsOverview />);
    expect(await screen.findByText("Too many requests")).toBeInTheDocument();
    expect(screen.getByText(/Wait 9 s/)).toBeInTheDocument();
  });
  it("35 5xx is sanitized and retryable", async () => {
    const user = userEvent.setup();
    let ok = false;
    mockFetch({ [URL_]: () => (ok ? opsBody() : errorResponse(500, "weird", "SELECT * FROM secrets")) });
    wrap(<OpsOverview />);
    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("SELECT");
    ok = true;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Overall sender governance health")).toBeInTheDocument();
  });
  it("36 network failure", async () => {
    mockFetch({});
    globalThis.fetch.mockImplementation(async () => { throw new TypeError("x"); });
    wrap(<OpsOverview />);
    expect(await screen.findByText("You seem to be offline")).toBeInTheDocument();
  });
});

describe("a11y and i18n", () => {
  it("29 summary cards are keyboard reachable links and Refresh works with Enter", async () => {
    const user = userEvent.setup();
    const f = mockFetch({ [URL_]: opsBody() });
    wrap(<OpsOverview />);
    await screen.findByText(/Generated/);
    await user.tab();
    expect(screen.getByRole("button", { name: "Refresh" })).toHaveFocus();
    const n = f.calls.length;
    await user.keyboard("{Enter}");
    await waitFor(() => expect(f.calls.length).toBe(n + 1));
    await user.tab();
    expect(document.activeElement.tagName).toBe("A");
  });
  it("30/31 English and Albanian", async () => {
    const user = userEvent.setup();
    mockFetch({ [URL_]: opsBody() });
    wrap(<><LangSwitch /><OpsOverview /></>);
    expect(await screen.findByText("Overall sender governance health")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Shqip" }));
    expect(await screen.findByText("Shëndeti i përgjithshëm i qeverisjes së sender-ave")).toBeInTheDocument();
    expect(screen.getByText("Gatishmëria Central", { selector: ".stat-l" })).toBeInTheDocument();
    expect(screen.getAllByText("e panjohur").length).toBeGreaterThan(0);
    expect(screen.getByText("Gjeneruar", { exact: false })).toBeInTheDocument();
  });
});
