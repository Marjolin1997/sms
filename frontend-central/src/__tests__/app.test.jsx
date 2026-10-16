import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import App from "../App.jsx";
import { api, getToken, setToken } from "../api.js";
import { ADMIN, OPERATOR, effective, errorResponse, mockFetch, pageOf, policy, sender } from "../test/helpers.js";

const base = { "GET /admin/senders": pageOf([]), "GET /admin/sender-policies": pageOf([]) };
const signIn = async (user, email = "admin@example.com", pw = "correct horse") => {
  await user.type(screen.getByLabelText("Email"), email);
  await user.type(screen.getByLabelText("Password"), pw);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
};
const nav = () => screen.findByRole("navigation", { name: "Main navigation" });

describe("Central login", () => {
  it("1 admin signs in via /auth/token then /auth/me; token stays in sessionStorage only", async () => {
    const user = userEvent.setup();
    const f = mockFetch({ "POST /auth/token": { access_token: "jwt.secret.value", token_type: "bearer", expires_in: 900 }, "GET /auth/me": ADMIN, ...base });
    render(<App />);
    await signIn(user);
    await nav();
    expect(f.calls[0]).toMatchObject({ method: "POST", path: "/auth/token", body: { email: "admin@example.com", password: "correct horse" } });
    expect(f.calls[0].headers.Authorization).toBeUndefined();
    expect(f.calls[1].headers.Authorization).toBe("Bearer jwt.secret.value");
    expect(getToken()).toBe("jwt.secret.value");
    expect(document.body.innerHTML).not.toContain("jwt.secret.value");
    expect(document.body.innerHTML).not.toContain("correct horse");
    expect(localStorage.length).toBe(1); // vetëm gjuha
    expect(screen.getByText("admin@example.com")).toBeInTheDocument();
    expect(screen.queryByText("Read-only access")).toBeNull();
  });
  it("2 bad login shows a safe message, keeps no token and clears the password", async () => {
    const user = userEvent.setup();
    mockFetch({ "POST /auth/token": errorResponse(401, "invalid_credentials", "invalid email or password") });
    render(<App />);
    await signIn(user, "x@example.com", "wrong");
    expect(await screen.findByRole("alert")).toHaveTextContent("That email or password isn't right.");
    expect(getToken()).toBe("");
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });
  it("4 operator signs in and sees the read-only badge", async () => {
    const user = userEvent.setup();
    mockFetch({ "POST /auth/token": { access_token: "t", token_type: "bearer", expires_in: 900 }, "GET /auth/me": OPERATOR, ...base });
    render(<App />);
    await signIn(user, "op@example.com");
    await nav();
    expect(screen.getByText("Read-only access")).toBeInTheDocument();
  });
  it("3/34 401 during a session clears the token and returns to login with a notice", async () => {
    setToken("t"); location.hash = "#dashboard";
    mockFetch({ "GET /auth/me": ADMIN, ...base });
    render(<App />);
    await nav();
    mockFetch({ "GET /admin/senders": errorResponse(401, "unauthorized", "x") });
    await api.get("/admin/senders").catch(() => {});
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByText("Your session has ended. Please sign in again.")).toBeInTheDocument();
    expect(getToken()).toBe("");
  });
  it("35 403 keeps the session", async () => {
    setToken("t");
    mockFetch({ "GET /auth/me": OPERATOR, ...base });
    render(<App />);
    await nav();
    mockFetch({ "POST /admin/sender-policies": errorResponse(403, "forbidden", "insufficient role") });
    await expect(api.post("/admin/sender-policies", {})).rejects.toMatchObject({ kind: "forbidden", message: "Your role is read-only for this action." });
    expect(screen.getByRole("navigation", { name: "Main navigation" })).toBeInTheDocument();
  });
  it("restores a session from sessionStorage and signs out", async () => {
    const user = userEvent.setup();
    setToken("t");
    mockFetch({ "GET /auth/me": ADMIN, ...base });
    render(<App />);
    await nav();
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
    expect(getToken()).toBe("");
  });
  it("a stored token that /auth/me rejects falls back to the login form", async () => {
    setToken("stale");
    mockFetch({ "GET /auth/me": errorResponse(401, "unauthorized", "x") });
    render(<App />);
    expect(await screen.findByLabelText("Email")).toBeInTheDocument();
    expect(getToken()).toBe("");
  });
});

describe("shell and navigation", () => {
  const boot = async (me, hash = "") => { setToken("t"); location.hash = hash; mockFetch({ "GET /auth/me": me, ...base }); render(<App />); return nav(); };
  it("admin and operator both see the Sender Governance navigation (and nothing from Enterprise)", async () => {
    for (const me of [ADMIN, OPERATOR]) {
      const n = await boot(me);
      expect(within(n).getByText("Sender Registry")).toBeInTheDocument();
      expect(within(n).getByText("Country Policies")).toBeInTheDocument();
      expect(within(n).queryByText("Wallet")).toBeNull();
      screen.getByRole("button", { name: "Sign out" });
      document.body.innerHTML = ""; setToken("");
    }
  });
  it("36 unknown routes and malformed ids give a 404 page", async () => {
    await boot(ADMIN, "#nope");
    expect(await screen.findByText("This page doesn't exist")).toBeInTheDocument();
    location.hash = "#senders/not-a-uuid";
    await waitFor(() => expect(screen.getByText("This page doesn't exist")).toBeInTheDocument());
    location.hash = "#<img src=x onerror=alert(1)>";
    await waitFor(() => expect(screen.getByText("This page doesn't exist")).toBeInTheDocument());
    expect(document.querySelector("img[src='x']")).toBeNull();
  });
  it("breadcrumbs and aria-current", async () => {
    const n = await boot(ADMIN, "#policies");
    expect(await screen.findByText("Explicit".length ? "Current policies" : "")).toBeInTheDocument();
    expect(within(n).getByRole("link", { name: /Country Policies/ })).toHaveAttribute("aria-current", "page");
    expect(within(screen.getByRole("navigation", { name: "Breadcrumb" })).getByText("Sender Governance")).toBeInTheDocument();
  });
  it("offline banner", async () => {
    await boot(ADMIN);
    window.dispatchEvent(new Event("offline"));
    expect(await screen.findByText(/You're offline/)).toBeInTheDocument();
  });
  it("42/43 English and Albanian", async () => {
    const user = userEvent.setup();
    mockFetch({});
    render(<App />);
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Shqip" }));
    expect(screen.getByRole("button", { name: "Hyr" })).toBeInTheDocument();
    expect(localStorage.getItem("central_lang")).toBe("sq");
    await user.click(screen.getByRole("button", { name: "English" }));
  });
  it("47 overview shows only what the API provides and says readiness is CLI-only", async () => {
    mockFetch({ "GET /auth/me": ADMIN, "GET /admin/senders": pageOf([sender(), sender()]), "GET /admin/sender-policies": pageOf([policy({ allowed: false }), policy({ requires_approval: false })]) });
    setToken("t"); location.hash = "#dashboard"; render(<App />);
    expect(await screen.findByText("Senders waiting for review")).toBeInTheDocument();
    const cards = document.querySelectorAll(".stat");
    expect([...cards].map((c) => c.querySelector(".stat-v").textContent)).toEqual(["2", "2", "1", "1"]);
    expect(screen.getByText(/isn't available over HTTP yet/)).toBeInTheDocument();
  });
});
