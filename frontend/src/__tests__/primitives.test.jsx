import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api.js";
import { AuditTimeline, Breadcrumbs, DetailList, DetailRow, Empty, ErrorBoundary, ErrorBox, ErrorState, Pagination, StatusBadge, Table, UiProvider, statusTone, useLoad, useUi } from "../ui.jsx";

describe("StatusBadge semantics", () => {
  it.each([
    ["active", "good"], ["approved", "good"], ["paid", "good"], ["delivered", "good"],
    ["pending", "warn"], ["suspended", "warn"], ["unpaid", "warn"],
    ["sent", "info"], ["processing", "info"],
    ["draft", "muted"],
    ["rejected", "bad"], ["revoked", "bad"], ["failed", "bad"], ["overdue", "bad"], ["policy_denied", "bad"],
  ])("%s → %s", (status, tone) => expect(statusTone(status)).toBe(tone));
  it("unknown statuses are neutral, never green", () => expect(statusTone("???")).toBe("muted"));
  it("never relies on colour alone: status text plus an aria-hidden symbol", () => {
    render(<><StatusBadge>approved</StatusBadge><StatusBadge>overdue</StatusBadge></>);
    const ok = screen.getByText("approved");
    expect(ok).toHaveClass("badge", "good");
    expect(ok.querySelector("i")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("overdue")).toHaveClass("bad");
  });
});

describe("error presentation", () => {
  it("ErrorState title follows the error kind and offers retry only when it makes sense", async () => {
    const retry = vi.fn();
    const { rerender } = render(<ErrorState error={new ApiError(0, "network", "x")} retry={retry} />);
    expect(screen.getByText("You seem to be offline")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalled();
    rerender(<ErrorState error={new ApiError(403, "forbidden", "x")} retry={retry} />);
    expect(screen.getByText("You don't have access")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });
  it("429 tells how long to wait", () => {
    render(<ErrorState error={new ApiError(429, "rate_limited", "x", { retryAfter: 9 })} />);
    expect(screen.getByText(/Wait 9 s/)).toBeInTheDocument();
  });
  it("5xx with unknown code never shows server text; known sender code does", () => {
    const { rerender } = render(<ErrorBox error={new ApiError(500, "weird", "Traceback: db password leaked")} />);
    expect(screen.getByRole("alert")).not.toHaveTextContent("Traceback");
    expect(screen.getByRole("alert")).toHaveTextContent("Something went wrong on our side");
    rerender(<ErrorBox error={new ApiError(409, "sender_authority_frozen", "internal")} />);
    expect(screen.getByRole("alert")).toHaveTextContent("managed centrally");
  });
  it("stale data keeps a visible notice", () => {
    render(<ErrorState stale error={new ApiError(503, "x", "y")} />);
    expect(screen.getByText("Showing the last data we received.")).toBeInTheDocument();
  });
  it("ErrorBoundary replaces a crashing child with a recoverable message", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const Boom = () => { throw new Error("secret stack"); };
    render(<ErrorBoundary><Boom /></ErrorBoundary>);
    expect(screen.getByRole("alert")).toHaveTextContent("This page hit an unexpected problem");
    expect(screen.getByRole("alert")).not.toHaveTextContent("secret stack");
  });
});

describe("loading / empty / table states", () => {
  it("Table: skeleton while loading, empty state with title, rows otherwise", () => {
    const cols = [{ label: "A", key: "a" }];
    const { rerender, container } = render(<Table cols={cols} rows={[]} loading />);
    expect(container.querySelector("[aria-busy]")).not.toBeNull();
    rerender(<Table cols={cols} rows={[]} emptyTitle="Nothing" empty="Add one" />);
    expect(screen.getByText("Nothing")).toBeInTheDocument();
    rerender(<Table cols={cols} rows={[{ id: 1, a: "x" }]} />);
    expect(screen.getByText("x")).toBeInTheDocument();
  });
  it("Empty renders title, text and action", () => {
    render(<Empty title="T" action={<button>Go</button>}>body</Empty>);
    expect(screen.getByRole("button", { name: "Go" })).toBeInTheDocument();
  });
});

describe("Pagination / Breadcrumbs / Detail / Timeline", () => {
  it("Pagination: hidden for a single page, prev/next otherwise, calls with offsets", async () => {
    const onChange = vi.fn();
    const { container, rerender } = render(<Pagination page={{ offset: 0, limit: 10, hasMore: false }} onChange={onChange} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<Pagination page={{ offset: 10, limit: 10, hasMore: true }} onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: "Next" }));
    await userEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(onChange.mock.calls).toEqual([[20], [0]]);
  });
  it("Pagination disables Previous on the first page", () => {
    render(<Pagination page={{ offset: 0, limit: 10, hasMore: true }} onChange={() => {}} />);
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
  });
  it("Breadcrumbs: links before the last, aria-current on the last", () => {
    render(<Breadcrumbs items={[{ label: "Setup", href: "#x" }, { label: "Senders" }]} />);
    expect(screen.getByRole("link", { name: "Setup" })).toBeInTheDocument();
    expect(screen.getByText("Senders").closest("li")).toHaveAttribute("aria-current", "page");
  });
  it("DetailRow + AuditTimeline", () => {
    render(<><DetailList><DetailRow label="Revision">4</DetailRow><DetailRow label="Reason" /></DetailList>
      <AuditTimeline events={[{ id: 1, at: new Date().toISOString(), title: "approve", status: "approved", actor: "ops", detail: "ok" }]} /></>);
    expect(screen.getByText("Revision").nextSibling).toHaveTextContent("4");
    expect(screen.getByText("Reason").nextSibling).toHaveTextContent("-");
    expect(screen.getByRole("list")).toHaveTextContent("approve");
    expect(screen.getByText("approved")).toHaveClass("good");
  });
  it("AuditTimeline shows an empty state", () => {
    render(<AuditTimeline events={[]} />);
    expect(screen.getByText("No history yet.")).toBeInTheDocument();
  });
});

describe("ConfirmDialog accessibility", () => {
  function Host() { const { confirm } = useUi(); return <button onClick={() => confirm({ title: "Sure?", confirmLabel: "Yes" })}>open</button>; }
  it("traps Tab inside the dialog, closes on Escape and restores focus", async () => {
    const user = userEvent.setup();
    render(<UiProvider><Host /></UiProvider>);
    const opener = screen.getByRole("button", { name: "open" });
    await user.click(opener);
    const dlg = screen.getByRole("dialog");
    expect(dlg).toHaveAttribute("aria-modal", "true");
    const [cancel, yes] = [screen.getByRole("button", { name: "Cancel" }), screen.getByRole("button", { name: "Yes" })];
    expect(yes).toHaveFocus();
    await user.tab();
    expect(cancel).toHaveFocus();
    await user.tab({ shift: true });
    expect(yes).toHaveFocus();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(opener).toHaveFocus();
  });
});

describe("useLoad()", () => {
  function Probe({ fn }) {
    const r = useLoad(fn, [fn]);
    return <div>{r.loading ? "loading" : r.error ? `error${r.stale ? "-stale" : ""}` : `data:${r.data}`}<button onClick={r.reload}>again</button></div>;
  }
  it("passes an AbortSignal and aborts on unmount", async () => {
    let sig;
    const fn = vi.fn((s) => { sig = s; return new Promise(() => {}); });
    const { unmount } = render(<Probe fn={fn} />);
    expect(sig.aborted).toBe(false);
    unmount();
    expect(sig.aborted).toBe(true);
  });
  it("an old slow response never overwrites a newer one", async () => {
    const resolvers = [];
    const fn = vi.fn(() => new Promise((res) => resolvers.push(res)));
    render(<Probe fn={fn} />);
    await userEvent.click(screen.getByRole("button", { name: "again" }));
    await act(async () => { resolvers[1]("new"); });
    await act(async () => { resolvers[0]("old"); });
    expect(screen.getByText("data:new")).toBeInTheDocument();
  });
  it("keeps previous data and flags it stale when a refresh fails", async () => {
    let n = 0;
    const fn = vi.fn(async () => { if (n++ === 0) return "v1"; throw new ApiError(503, "x", "y"); });
    render(<Probe fn={fn} />);
    expect(await screen.findByText("data:v1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "again" }));
    expect(await screen.findByText("error-stale")).toBeInTheDocument();
  });
});
