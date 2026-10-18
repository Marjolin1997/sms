import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import App from "../App.jsx";
import { setKey } from "../api.js";
import { CLIENT, STAFF, errorResponse, mockFetch } from "../test/helpers.js";

const dashboardMocks = {
  "GET /v1/portal/overview": { wallets: [], sms: {}, email: {}, campaigns: {}, webhooks: {} },
  "GET /v1/portal/onboarding": { done: true, steps: [] },
  "GET /v1/messages": { items: [], next_before_id: null },
  "GET /v1/inbox/unread": { unread: 0 },
  "GET /v1/admin/stats": { oldest_queued_age_seconds: null, stuck_sending: 0, dlr_problems_24h: {}, switches: [] },
  "GET /v1/admin/accounts": [],
};

async function login(user, key = "sms_abc_secret") {
  await user.type(screen.getByLabelText("API key"), key);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("login", () => {
  it("shows the sign-in form and blocks empty submit", () => {
    mockFetch({});
    render(<App />);
    expect(screen.getByRole("button", { name: "Sign in" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "English" })).toBeInTheDocument();
  });

  it("shows a friendly message for an invalid key and does not keep it", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /v1/me": errorResponse(401, "unauthorized", "invalid credentials") });
    render(<App />);
    await login(user, "sms_bad_key");
    expect(await screen.findByRole("alert")).toHaveTextContent("isn't valid");
    expect(sessionStorage.getItem("sms_key")).toBeNull();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("signs a client in and shows only client menus (no Staff group)", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /v1/me": CLIENT, ...dashboardMocks });
    render(<App />);
    await login(user);
    const nav = await screen.findByRole("navigation");
    for (const label of ["Overview", "Send", "Message history", "Campaigns", "Contacts", "Wallet", "Billing", "API keys", "Inbox"])
      expect(within(nav).getByText(label)).toBeInTheDocument();
    expect(within(nav).queryByText("Approvals")).not.toBeInTheDocument();
    expect(within(nav).queryByText("Staff")).not.toBeInTheDocument();
    expect(screen.getByText("acme")).toBeInTheDocument();
  });

  it("gives staff the Staff group first and asks them to pick an account for account pages", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /v1/me": STAFF, ...dashboardMocks });
    render(<App />);
    await login(user);
    const nav = await screen.findByRole("navigation");
    expect(within(nav).getByText("Approvals")).toBeInTheDocument();
    expect(within(nav).getByText("Security")).toBeInTheDocument();
    const labels = within(nav).getAllByRole("link").map((a) => a.textContent);
    const at = (name) => labels.findIndex((l) => l.endsWith(name));
    expect(at("Approvals")).toBeGreaterThan(-1);
    expect(at("Approvals")).toBeLessThan(at("Send"));
    location.hash = "messages";
    expect(await screen.findByText(/Choose an account above/)).toBeInTheDocument();
  });

  it("warns staff when two-factor is required but not yet enabled", async () => {
    const user = userEvent.setup();
    mockFetch({ "GET /v1/me": { ...STAFF, two_factor_required: true }, ...dashboardMocks });
    render(<App />);
    await login(user);
    expect(await screen.findByText(/Two-factor is required for sensitive actions/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Set it up now" })).toHaveAttribute("href", "#security");
  });

  it("restores a session from sessionStorage and can sign out", async () => {
    const user = userEvent.setup();
    setKey("sms_saved_key");
    mockFetch({ "GET /v1/me": CLIENT, ...dashboardMocks });
    render(<App />);
    expect(await screen.findByRole("navigation")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(screen.getByLabelText("API key")).toBeInTheDocument());
    expect(sessionStorage.getItem("sms_key")).toBeNull();
  });

  it("switches the console to Albanian", async () => {
    const user = userEvent.setup();
    mockFetch({});
    render(<App />);
    await user.click(screen.getByRole("button", { name: "Shqip" }));
    expect(screen.getByRole("button", { name: "Hyr" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "English" }));
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });
});
