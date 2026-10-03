import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { I18nProvider } from "../i18n.jsx";
import Send from "../pages/Send.jsx";
import { UiProvider } from "../ui.jsx";
import { mockFetch } from "../test/helpers.js";

const wrap = (ui) => render(<I18nProvider><UiProvider>{ui}</UiProvider></I18nProvider>);
const QUOTE = { segments: 1, encoding: "GSM-7", unit_price: "0.045", total: "0.045", currency: "EUR" };

function routes(extra = {}) {
  return {
    "GET /v1/sender-ids": [{ id: 1, value: "ACME", status: "approved", country: "AL" }],
    "GET /v1/templates": [],
    "GET /v1/wallets": [{ id: 1, currency: "EUR", available: "10.00", held: "0" }],
    "POST /v1/messages/quote": QUOTE,
    ...extra,
  };
}

describe("Send SMS", () => {
  it("explains what to do when there is no approved sender ID", async () => {
    mockFetch(routes({ "GET /v1/sender-ids": [] }));
    wrap(<Send />);
    expect(await screen.findByText("You need an approved sender ID")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Request a sender ID" })).toHaveAttribute("href", "#senders");
  });

  it("validates the number, shows the live price and sends with an Idempotency-Key", async () => {
    const user = userEvent.setup();
    const f = mockFetch(routes({ "POST /v1/messages": { id: "abc-1", status: "queued", to: "355691234567", total_price: "0.045", currency: "EUR" } }));
    wrap(<Send />);
    const to = await screen.findByPlaceholderText("+355691234567");
    const send = screen.getByRole("button", { name: /Send SMS/ });
    expect(send).toBeDisabled();
    await user.type(to, "0691234567");
    expect(send).toBeDisabled();
    await user.clear(to);
    await user.type(to, "+355691234567");
    await user.type(screen.getByRole("textbox", { name: /Message/ }), "Hello there");
    expect((await screen.findAllByText(/0\.045/, {}, { timeout: 3000 })).length).toBeGreaterThan(0);
    expect(screen.getByText(/11 characters/)).toBeInTheDocument();
    await waitFor(() => expect(send).toBeEnabled());
    await user.click(send);
    const post = await waitFor(() => {
      const c = f.calls.find((x) => x.method === "POST" && x.path === "/v1/messages");
      expect(c).toBeTruthy();
      return c;
    });
    expect(post.body).toMatchObject({ to: "+355691234567", sender: "ACME", text: "Hello there" });
    expect(post.headers["Idempotency-Key"]).toMatch(/[0-9a-f-]{8,}/);
    expect(await screen.findByText("Message accepted for delivery")).toBeInTheDocument();
  });

  it("switches to Unicode when the text needs it", async () => {
    const user = userEvent.setup();
    mockFetch(routes());
    wrap(<Send />);
    await user.type(await screen.findByRole("textbox", { name: /Message/ }), "Mirë se vini çelësi");
    expect(await screen.findByText(/Unicode/)).toBeInTheDocument();
  });
});
