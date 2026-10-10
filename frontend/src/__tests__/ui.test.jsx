import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Badge, Table, UiProvider, ago, money, smsInfo, useUi } from "../ui.jsx";

describe("smsInfo()", () => {
  it("counts GSM-7 characters and segments (160 / 153)", () => {
    expect(smsInfo("")).toMatchObject({ encoding: "GSM-7", segments: 0, length: 0 });
    expect(smsInfo("a".repeat(160))).toMatchObject({ segments: 1, ucs: false });
    expect(smsInfo("a".repeat(161))).toMatchObject({ segments: 2, perSegment: 153 });
  });
  it("counts extension characters as two", () => {
    expect(smsInfo("€").length).toBe(2);
    expect(smsInfo("{}[]").length).toBe(8);
  });
  it("switches to Unicode (70 / 67) for characters outside GSM-7", () => {
    const r = smsInfo("ç".repeat(71));
    expect(r).toMatchObject({ encoding: "Unicode", ucs: true, segments: 2, perSegment: 67 });
    expect(smsInfo("ç".repeat(70)).segments).toBe(1);
  });
  it("counts emoji as two UCS-2 units", () => {
    expect(smsInfo("😀").length).toBe(2);
  });
});

describe("formatters", () => {
  it("money() keeps 2-4 decimals and dashes for null", () => {
    expect(money("0.045")).toBe("0.045");
    expect(money(5)).toBe("5.00");
    expect(money(null)).toBe("-");
  });
  it("ago() is relative for recent times", () => {
    expect(ago(null)).toBe("-");
    expect(ago(new Date().toISOString())).toBe("just now");
    expect(ago(new Date(Date.now() - 5 * 60000).toISOString())).toBe("5 min ago");
    expect(ago(new Date(Date.now() - 3 * 3600000).toISOString())).toBe("3 h ago");
  });
});

describe("<Badge>", () => {
  it("maps statuses to tones", () => {
    const { rerender } = render(<Badge>delivered</Badge>);
    expect(screen.getByText("delivered")).toHaveClass("good");
    rerender(<Badge>failed</Badge>);
    expect(screen.getByText("failed")).toHaveClass("bad");
    rerender(<Badge>whatever</Badge>);
    expect(screen.getByText("whatever")).toHaveClass("muted");
  });
});

describe("<Table>", () => {
  it("shows the empty state with title and text", () => {
    render(<Table rows={[]} cols={[]} emptyTitle="Nothing yet" empty="Add one." />);
    expect(screen.getByText("Nothing yet")).toBeInTheDocument();
    expect(screen.getByText("Add one.")).toBeInTheDocument();
  });
  it("renders rows, custom cells and click handlers", async () => {
    const onClick = vi.fn();
    render(<Table rows={[{ id: 1, name: "Ana", _onClick: onClick }]} cols={[{ label: "Name", key: "name" }, { label: "Up", render: (r) => r.name.toUpperCase() }]} />);
    expect(screen.getByRole("columnheader", { name: "Name" })).toBeInTheDocument();
    expect(screen.getByText("ANA")).toBeInTheDocument();
    await userEvent.click(screen.getByText("Ana"));
    expect(onClick).toHaveBeenCalled();
  });
  it("shows a skeleton while loading with no rows", () => {
    const { container } = render(<Table rows={[]} cols={[]} loading />);
    expect(container.querySelector(".skeleton")).toBeInTheDocument();
  });
});

function Asker({ opts, onResult }) {
  const { confirm } = useUi();
  return <button onClick={async () => onResult(await confirm(opts))}>ask</button>;
}

describe("confirm dialog", () => {
  it("resolves true on confirm and false on Cancel / Escape", async () => {
    const user = userEvent.setup();
    const results = [];
    render(<UiProvider><Asker opts={{ title: "Sure?", body: "Really." }} onResult={(r) => results.push(r)} /></UiProvider>);
    await user.click(screen.getByText("ask"));
    expect(screen.getByRole("dialog")).toHaveAccessibleName("Sure?");
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(results).toEqual([true]));
    await user.click(screen.getByText("ask"));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(results).toEqual([true, false]));
    await user.click(screen.getByText("ask"));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(results).toEqual([true, false, false]));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("requires a reason of the minimum length before confirming", async () => {
    const user = userEvent.setup();
    const results = [];
    render(<UiProvider><Asker opts={{ title: "Reject", input: "Reason", inputRequired: true, minLength: 3, confirmLabel: "Reject" }} onResult={(r) => results.push(r)} /></UiProvider>);
    await user.click(screen.getByText("ask"));
    const ok = screen.getByRole("button", { name: "Reject" });
    expect(ok).toBeDisabled();
    await user.type(screen.getByLabelText("Reason"), "ab");
    expect(ok).toBeDisabled();
    await user.type(screen.getByLabelText("Reason"), "c ");
    expect(ok).toBeEnabled();
    await user.click(ok);
    await waitFor(() => expect(results).toEqual(["abc"]));
  });
});
