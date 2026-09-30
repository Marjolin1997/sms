import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { I18nProvider } from "../i18n.jsx";
import Providers from "../pages/Providers.jsx";
import { UiProvider } from "../ui.jsx";
import { mockFetch } from "../test/helpers.js";

const wrap = (ui) => render(<I18nProvider><UiProvider>{ui}</UiProvider></I18nProvider>);
const P = (o) => ({ provider: "twilio", total: 50, delivered: 20, failed: 20, in_flight: 10, delivery_rate: 0.5, unknown_outcome: 2,
  top_errors: [{ code: "twilio_30003", count: 18 }], last_delivered_at: new Date().toISOString(), oldest_in_flight_seconds: 900, ...o });

describe("Provider health page", () => {
  it("warns about a low delivery rate, unknown outcomes and stuck messages", async () => {
    mockFetch({ "GET /v1/admin/providers": { hours: 24, providers: [P()] }, "GET /v1/admin/messages/unresolved": [{ id: "abc-1", owner_ref: "acme", provider: "twilio", to: "+35569…03", sender: "ACME", total_price: "0.05", currency: "EUR", created_at: new Date().toISOString() }] });
    wrap(<Providers />);
    expect(await screen.findByText("Delivery rate is low")).toBeInTheDocument();
    expect(screen.getByText("2 with unknown outcome")).toBeInTheDocument();
    expect(screen.getByText("50.0%")).toBeInTheDocument();
    expect(screen.getByText("twilio_30003")).toBeInTheDocument();
    expect(screen.getByText("oldest 15 min")).toBeInTheDocument();
    expect(await screen.findByText("+35569…03")).toBeInTheDocument();
  });

  it("does not raise the alarm on a small sample and shows the empty states", async () => {
    mockFetch({ "GET /v1/admin/providers": { hours: 24, providers: [P({ total: 4, delivered: 1, failed: 3, in_flight: 0, unknown_outcome: 0, oldest_in_flight_seconds: null, delivery_rate: 0.25, top_errors: [] })] }, "GET /v1/admin/messages/unresolved": [] });
    wrap(<Providers />);
    expect(await screen.findByText("25.0%")).toBeInTheDocument();
    expect(screen.queryByText("Delivery rate is low")).not.toBeInTheDocument();
    expect(await screen.findByText("Nothing to reconcile")).toBeInTheDocument();
  });
});
