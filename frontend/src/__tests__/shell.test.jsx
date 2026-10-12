import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import App from "../App.jsx";
import { api, setKey } from "../api.js";
import { CLIENT, STAFF, errorResponse, mockFetch } from "../test/helpers.js";

const mocks = {
  "GET /v1/portal/overview": { wallets: [], sms: {}, email: {}, campaigns: {}, webhooks: {} },
  "GET /v1/portal/onboarding": { done: true, steps: [] },
  "GET /v1/messages": { items: [], next_before_id: null },
  "GET /v1/inbox/unread": { unread: 0 },
  "GET /v1/admin/stats": { oldest_queued_age_seconds: null, stuck_sending: 0, dlr_problems_24h: {}, switches: [] },
  "GET /v1/admin/accounts": [],
  "GET /v1/sender-ids": [],
  "GET /v1/templates": [],
};
const open = async (me, hash = "") => {
  setKey("sms_saved_key");
  location.hash = hash;
  mockFetch({ "GET /v1/me": me, ...mocks });
  render(<App />);
  return screen.findByRole("navigation", { name: "Main navigation" });
};

describe("route guards", () => {
  it("shows a 403 page (not a silent redirect) when a customer deep-links to a staff page", async () => {
    await open(CLIENT, "#approvals");
    expect(await screen.findByText("You don't have access to this page")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("No access");
    expect(screen.getByRole("link", { name: "Back to overview" })).toHaveAttribute("href", "#dashboard");
  });
  it("shows a 404 page for unknown routes and never evaluates hostile hashes", async () => {
    await open(STAFF, "#does-not-exist");
    expect(await screen.findByText("This page doesn't exist")).toBeInTheDocument();
    location.hash = "#<img src=x onerror=alert(1)>";
    await waitFor(() => expect(screen.getByText("This page doesn't exist")).toBeInTheDocument());
    expect(document.querySelector("img[src='x']")).toBeNull();
  });
  it("lazy-loads an allowed page and shows breadcrumbs for grouped routes", async () => {
    await open(CLIENT, "#senders");
    expect(await screen.findByText("Request a sender ID")).toBeInTheDocument();
    const crumbs = screen.getByRole("navigation", { name: "Breadcrumb" });
    expect(within(crumbs).getByText("Setup")).toBeInTheDocument();
    expect(within(crumbs).getByText("Sender IDs & templates")).toHaveAttribute("aria-current", undefined);
  });
  it("marks the current page in the menu (aria-current)", async () => {
    const nav = await open(CLIENT, "#senders");
    expect(within(nav).getByRole("link", { name: /Sender IDs/ })).toHaveAttribute("aria-current", "page");
  });
});

describe("auth expiry", () => {
  it("401 during a session returns to sign-in with a notice and clears the key", async () => {
    await open(CLIENT, "#dashboard");
    mockFetch({ "GET /v1/thing": errorResponse(401, "unauthorized", "expired") });
    await api.get("/v1/thing").catch(() => {});
    expect(await screen.findByLabelText("API key")).toBeInTheDocument();
    expect(screen.getByText("Your session has ended. Please sign in again.")).toBeInTheDocument();
    expect(sessionStorage.getItem("sms_api_key")).toBeNull();
  });
  it("403 keeps the session (no logout)", async () => {
    await open(CLIENT, "#dashboard");
    mockFetch({ "GET /v1/thing": errorResponse(403, "forbidden", "no") });
    await api.get("/v1/thing").catch(() => {});
    expect(screen.getByRole("navigation", { name: "Main navigation" })).toBeInTheDocument();
  });
  it("the key never appears in the DOM after sign-in", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /v1/me": CLIENT, ...mocks });
    render(<App />);
    await user.type(screen.getByLabelText("API key"), "sms_topsecret_value");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("navigation", { name: "Main navigation" });
    expect(document.body.innerHTML).not.toContain("sms_topsecret_value");
  });
});

describe("global states", () => {
  it("shows an offline banner", async () => {
    await open(CLIENT, "#dashboard");
    window.dispatchEvent(new Event("offline"));
    expect(await screen.findByText(/You're offline/)).toBeInTheDocument();
    window.dispatchEvent(new Event("online"));
    await waitFor(() => expect(screen.queryByText(/You're offline/)).not.toBeInTheDocument());
  });
});
