import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { I18nProvider, LangSwitch } from "../i18n.jsx";
import OpsAlerts from "../pages/OpsAlerts.jsx";
import OpsBootstrap from "../pages/OpsBootstrap.jsx";
import OpsOverview from "../pages/OpsOverview.jsx";
import OpsSync from "../pages/OpsSync.jsx";
import { ENT, META, errorResponse, mockFetch, opsBody, replaceChecks, withEnterprise } from "../test/helpers.js";
import { UiProvider } from "../ui.jsx";

const URL_ = "GET /admin/operations/sender";
const wrap = (ui) => render(<I18nProvider><UiProvider>{ui}</UiProvider></I18nProvider>);
const row = (label) => screen.getByText(label).nextSibling;

describe("Enterprise snapshot evidence replaces UNKNOWN with real values", () => {
  it("40 sync: cursor, latest Central sequence, lag, last success, snapshot age, gap recoveries", async () => {
    mockFetch({ [URL_]: withEnterprise(opsBody()) });
    wrap(<OpsSync />);
    await screen.findByText("Enterprise cursor");
    expect(row("Enterprise cursor")).toHaveTextContent("40");
    expect(row("Latest Central sequence known")).toHaveTextContent("45");
    expect(row("Lag")).toHaveTextContent("5");
    expect(row("Last sync success")).toHaveTextContent("30 s ago");
    expect(row("Snapshot age")).toHaveTextContent("10 min");
    expect(row("Gap recoveries")).toHaveTextContent("1");
    expect(row("Failure count")).toHaveTextContent("2");
    expect(row("Withdrawn authorizations")).toHaveTextContent("1");
    expect(screen.queryByText("Not connected to Central: evidence unavailable here.")).toBeNull();
  });
  it("41 request reporter: pending, retry, sending, failed, oldest open, last success, permanent failures", async () => {
    mockFetch({ [URL_]: withEnterprise(opsBody()) });
    wrap(<OpsSync />);
    await screen.findByText("Oldest open request");
    expect(row("Pending")).toHaveTextContent("3");
    expect(row("Retry")).toHaveTextContent("2");
    expect(row("Sending")).toHaveTextContent("1");
    expect(row("Failed")).toHaveTextContent("4");
    expect(row("Oldest open request")).toHaveTextContent("10 min");
    expect(row("Last successful report")).toHaveTextContent("45 s ago");
    expect(row("Permanent failures by category")).toHaveTextContent("http_422: 3 · transport: 1");
    const fail = screen.getByText("Enterprise request outbox").closest("li");
    expect(within(fail).getByText("fail")).toHaveClass("bad");
    expect(within(fail).getByText("Enterprise")).toBeInTheDocument(); // etiketa e burimit
  });
  it("zero and 'none' are shown as values, never as unknown", async () => {
    const e = ENT();
    e.requests = { ...e.requests, pending: 0, failed: 0, oldest_open_age_seconds: null, last_sent_age_seconds: null, failed_by_category: {} };
    mockFetch({ [URL_]: withEnterprise(opsBody(), { ent: e }) });
    wrap(<OpsSync />);
    await screen.findByText("Oldest open request");
    expect(row("Failed")).toHaveTextContent("0");
    expect(row("Oldest open request")).toHaveTextContent("None open");
    expect(row("Last successful report")).toHaveTextContent("Never");
  });
  it("42 shadow: sample progress, match rate, critical drift and every drift category", async () => {
    mockFetch({ [URL_]: withEnterprise(opsBody()) });
    wrap(<OpsBootstrap />);
    await screen.findByText("Sample progress");
    expect(row("Sample progress")).toHaveTextContent("250 of 100 required");
    expect(row("Match rate")).toHaveTextContent("96.00 %");
    expect(row("Critical drift")).toHaveTextContent("7");
    expect(row("Central missing")).toHaveTextContent("3");
    expect(row("Local allow, Central deny")).toHaveTextContent("4");
    expect(row("Local deny, Central allow")).toHaveTextContent("2");
    expect(row("Policy mismatch")).toHaveTextContent("1");
    expect(row("Identity mismatch")).toHaveTextContent("2");
    expect(screen.getByText("Shadow comparison and drift").closest("li")).toHaveTextContent("7 critical drift");
  });
  it("43 bootstrap: Enterprise state, bounded categories and unknown ones as Other", async () => {
    mockFetch({ [URL_]: withEnterprise(opsBody()) });
    wrap(<OpsBootstrap />);
    await screen.findByText("Open issues by category");
    const list = screen.getByText("Open issues by category").nextSibling;
    expect(within(list).getByText("Identity conflict").parentElement).toHaveTextContent("2");
    expect(within(list).getByText("Other / unknown").parentElement).toHaveTextContent("1");
    expect(screen.queryByText(/brand_new_category/)).toBeNull();
    expect(screen.getByText("abcdef012345")).toBeInTheDocument();
    expect(row("Acknowledgement valid")).toHaveTextContent("No");
    expect(row("Current authority")).toHaveTextContent("shadow");
  });
  it("evidence disagreement is a visible WARN and never silently resolved", async () => {
    mockFetch({ [URL_]: withEnterprise(opsBody()) });
    wrap(<OpsBootstrap />);
    const li = (await screen.findByText("Bootstrap evidence agreement")).closest("li");
    expect(within(li).getByText("warn")).toHaveClass("warn");
    expect(li).toHaveTextContent("Central and Enterprise bootstrap evidence disagree.");
    expect(li).toHaveTextContent("central_senders: 10 · enterprise_senders: 12");
  });
});

describe("freshness, unknown and partial availability", () => {
  it("44 a stale snapshot is shown as stale (WARN) with the server's age and nothing derived says pass", async () => {
    mockFetch({ [URL_]: withEnterprise(opsBody(), { state: "stale" }) });
    wrap(<OpsOverview />);
    await screen.findByText("Snapshot status");
    expect(row("Snapshot status")).toHaveTextContent("warn");
    expect(row("Snapshot status")).toHaveTextContent("stale");
    expect(row("Snapshot age")).toHaveTextContent("15 min");
    expect(row("Stale after")).toHaveTextContent("5 min");
    expect(screen.getByLabelText("Enterprise snapshot: warn")).toBeInTheDocument();
    expect(row("Reporter")).toHaveTextContent("ent-prod");
    expect(row("Enterprise revision")).toHaveTextContent("8c820ff");
  });
  it("an expired snapshot is FAIL", async () => {
    mockFetch({ [URL_]: withEnterprise(opsBody(), { state: "expired" }) });
    wrap(<OpsOverview />);
    expect(await screen.findByLabelText("Enterprise snapshot: fail")).toBeInTheDocument();
  });
  it("45 without a snapshot: everything Enterprise is UNKNOWN with a clear explanation", async () => {
    mockFetch({ [URL_]: opsBody({ metrics: { ...opsBody().metrics, enterprise_snapshot: { present: false, deployments: 0 }, enterprise: null } }) });
    wrap(<OpsOverview />);
    expect(await screen.findByText(/No Enterprise snapshot has been received/)).toBeInTheDocument();
    expect(screen.getByLabelText("Enterprise snapshot: unknown")).toBeInTheDocument();
    expect(screen.getByLabelText("Critical drift: unknown")).toBeInTheDocument();
  });
  it("an unsupported snapshot version is explained, not an error", async () => {
    const b = opsBody({ metrics: { ...opsBody().metrics, enterprise_snapshot: META({ supported: false, state: null }), enterprise: null } });
    mockFetch({ [URL_]: b });
    wrap(<OpsOverview />);
    expect(await screen.findByText(/uses an unsupported version/)).toBeInTheDocument();
  });
  it("46 partial data: sync present, request reporter unavailable → unknown only for that part", async () => {
    const e = ENT();
    e.requests = null;
    const b = replaceChecks(withEnterprise(opsBody(), { ent: e }), "requests", [{ code: "enterprise_request_outbox", status: "unknown", summary: "Request reporter evidence was unavailable in the snapshot.", details: {}, source: "enterprise" }]);
    mockFetch({ [URL_]: b });
    wrap(<OpsSync />);
    await screen.findByText("Enterprise cursor");
    expect(row("Enterprise cursor")).toHaveTextContent("40");
    expect(screen.getByText("Enterprise request outbox").closest("li")).toHaveTextContent("unavailable in the snapshot");
    expect(screen.getByText("Enterprise request reporter (reported snapshot)").nextSibling).toHaveTextContent("Not connected to Central");
  });
  it("overview cards carry real server values and the overall FAIL", async () => {
    mockFetch({ [URL_]: withEnterprise(opsBody()) });
    wrap(<OpsOverview />);
    await screen.findByText("Overall sender governance health");
    expect(screen.getByLabelText("Critical drift: fail")).toHaveTextContent("Critical: 7");
    expect(screen.getByLabelText("Sync feed: pass")).toHaveTextContent("Lag: 5");
    expect(screen.getByLabelText("Request intake: fail")).toHaveTextContent("Pending: 3");
    expect(screen.getByLabelText("Cutover evidence: warn")).toHaveTextContent("Authority: shadow");
    expect(screen.getByRole("status", { name: "Overall sender governance health" }).querySelector(".badge")).toHaveClass("bad");
  });
});

describe("alerts and read-only behaviour", () => {
  it("alerts show their source (Central/Enterprise) and bounded names", async () => {
    mockFetch({ [URL_]: withEnterprise(opsBody({ alerts: [{ code: "bootstrap_unresolved", severity: "warning", check: "bootstrap_unresolved", section: "bootstrap", source: "central", summary: "3 unresolved", observed_at: "2026-03-01T10:00:00+00:00" }] })) });
    wrap(<OpsAlerts />);
    await screen.findByText("Critical shadow drift");
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows).toHaveLength(3);
    expect(within(rows[0]).getByText("Central")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Enterprise")).toBeInTheDocument();
    expect(within(rows[1]).getByText("critical")).toHaveClass("bad");
    expect(screen.getByRole("columnheader", { name: "Source" })).toBeInTheDocument();
  });
  it("47 no mutation controls anywhere on the Enterprise-evidence pages", async () => {
    const f = mockFetch({ [URL_]: withEnterprise(opsBody()) });
    for (const P of [OpsOverview, OpsSync, OpsBootstrap, OpsAlerts]) {
      const r = wrap(<P />);
      await screen.findAllByText(/Generated/);
      expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Refresh"]);
      r.unmount();
    }
    expect(f.calls.every((c) => c.method === "GET")).toBe(true);
  });
  it("51 responsive: the Source column is part of the table cells and secondary columns stay hideable", async () => {
    mockFetch({ [URL_]: withEnterprise(opsBody()) });
    wrap(<OpsAlerts />);
    const tr = (await screen.findByText("Critical shadow drift")).closest("tr");
    expect([...tr.querySelectorAll("td.hide-sm")].map((td) => td.dataset.label)).toEqual(["Check", "Section"]);
    expect(tr.querySelector("td[data-label='Source']")).not.toBeNull();
  });
  it("errors on the merged endpoint stay sanitized", async () => {
    mockFetch({ [URL_]: errorResponse(500, "weird", "secret trace") });
    wrap(<OpsOverview />);
    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain("secret trace");
  });
});

describe("i18n for the Enterprise evidence", () => {
  it("49/50 English and Albanian labels", async () => {
    const user = userEvent.setup();
    mockFetch({ [URL_]: withEnterprise(opsBody()) });
    wrap(<><LangSwitch /><OpsBootstrap /></>);
    expect(await screen.findByText("Sample progress")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Shqip" }));
    expect(await screen.findByText("Progresi i mostrave")).toBeInTheDocument();
    expect(screen.getByText("Mospërputhje identiteti")).toBeInTheDocument();
    expect(screen.getByText("Tjetër / e panjohur")).toBeInTheDocument();
    expect(screen.getByText("Konflikt identiteti")).toBeInTheDocument();
    expect(screen.getByText("Evidenca bootstrap nuk përputhet".length ? "Përputhja e evidencës bootstrap" : "")).toBeInTheDocument();
  });
});
