import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AuditLog } from "../src/components/AuditLog";
import { ApiError, api } from "../src/lib/api";
import type { AuditEntry, AuditPage } from "../src/lib/types";

vi.mock("../src/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/api")>()),
  api: { audit: vi.fn() },
}));

const audit = vi.mocked(api.audit);

let nextId = 0;
const entry = (over: Partial<AuditEntry> = {}): AuditEntry => {
  nextId += 1;
  return {
    id: nextId,
    run_id: "r1",
    tool_use_id: `t${nextId}`,
    tool: "write_note",
    tier: "write",
    input_preview: '{"title":"Todo","content":"milk"}',
    outcome: "approved",
    reason: "",
    result: "ok",
    requested_at: 1_700_000_000,
    decided_at: 1_700_000_004,
    run_text: "save it",
    ...over,
  };
};

const page = (items: AuditEntry[], over: Partial<AuditPage> = {}): AuditPage => ({
  items,
  total: items.length,
  counts: { approved: items.length },
  ...over,
});

const currentRows = () => within(screen.getByRole("list")).getAllByRole("listitem");
const rows = async () => {
  await screen.findByRole("list");
  return currentRows();
};

beforeEach(() => {
  audit.mockReset();
  nextId = 0;
});

describe("AuditLog", () => {
  it("shows what happened to each call and whether it then ran", async () => {
    audit.mockResolvedValue(
      page([
        entry({ outcome: "approved", result: "ok" }),
        entry({ outcome: "approved", result: "error" }),
        entry({ outcome: "denied", result: null, reason: "the user denied this action" }),
        entry({ outcome: "timed_out", result: null, reason: "no response within 300s" }),
        entry({ outcome: "auto", result: "ok", decided_at: 1_700_000_000 }),
      ]),
    );
    render(<AuditLog />);

    const list = within(await screen.findByRole("list"));
    expect(list.getAllByText("Approved")).toHaveLength(2);
    expect(list.getByText("Denied")).toBeTruthy();
    expect(list.getByText("Timed out")).toBeTruthy();
    expect(list.getByText("Auto-allowed")).toBeTruthy();
    expect(list.getAllByText("Ran")).toHaveLength(2);
    expect(list.getByText("Failed")).toBeTruthy(); // allowed, but it did not take effect
    expect(list.getAllByText("Not run")).toHaveLength(2);
  });

  it("keeps the details of a call one click away", async () => {
    audit.mockResolvedValue(
      page([
        entry({ outcome: "timed_out", result: null, reason: "no response within 300s", decided_at: 1_700_000_300 }),
        entry({ outcome: "approved", decided_at: 1_700_000_004 }),
        entry({ outcome: "auto", decided_at: 1_700_000_000, run_text: null }),
      ]),
    );
    render(<AuditLog />);

    const [timedOut, approved, auto] = await rows();
    expect(within(timedOut).getByText("no response within 300s")).toBeTruthy();
    expect(within(timedOut).getByText("No answer after 5 min.")).toBeTruthy();
    expect(within(approved).getByText("Answered after 4.0 s.")).toBeTruthy();
    expect(within(approved).getByText("save it")).toBeTruthy(); // the prompt it came from
    expect(within(auto).queryByText(/after/)).toBeNull(); // never had to wait for anyone
    expect(within(auto).queryByText("Asked in")).toBeNull(); // its chat is gone
  });

  it("filters by outcome on the server, offering only outcomes that exist", async () => {
    audit.mockImplementation(async (q) =>
      q?.outcome === "denied"
        ? page([entry({ outcome: "denied", result: null })], { total: 1, counts: { approved: 2, denied: 1 } })
        : page([entry(), entry(), entry({ outcome: "denied", result: null })], { counts: { approved: 2, denied: 1 } }),
    );
    render(<AuditLog />);

    expect((await screen.findByRole("button", { name: "All 3" })).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Approved 2" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Timed out/ })).toBeNull(); // none to show

    await userEvent.click(screen.getByRole("button", { name: "Denied 1" }));
    expect(audit).toHaveBeenLastCalledWith({ limit: 25, outcome: "denied" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Denied 1" }).getAttribute("aria-pressed")).toBe("true"));
    expect(await rows()).toHaveLength(1);
  });

  it("loads more, without repeating rows that moved while it was open", async () => {
    const [newest, middle, oldest] = [entry({ tool: "tool_c" }), entry({ tool: "tool_b" }), entry({ tool: "tool_a" })];
    audit
      .mockResolvedValueOnce(page([newest, middle], { total: 3 }))
      .mockResolvedValueOnce(page([middle, oldest], { total: 3 })); // `middle` shifted onto the second page
    render(<AuditLog />);

    expect(await rows()).toHaveLength(2);
    expect(screen.getByText("Showing 2 of 3")).toBeTruthy();

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(currentRows()).toHaveLength(3));
    expect(audit).toHaveBeenLastCalledWith({ limit: 25, offset: 2, outcome: undefined });
    expect(screen.getByText("Showing 3 of 3")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("says so when more cannot be loaded, and lets the user try again", async () => {
    audit
      .mockResolvedValueOnce(page([entry()], { total: 2 }))
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(page([entry()], { total: 2 }));
    render(<AuditLog />);

    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Could not load more.");
    expect(await rows()).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(currentRows()).toHaveLength(2));
  });

  it("explains an empty log", async () => {
    audit.mockResolvedValue(page([], { counts: {} }));
    render(<AuditLog />);
    expect(await screen.findByText(/Nothing recorded yet/)).toBeTruthy();
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("shows the server's error", async () => {
    audit.mockRejectedValue(new ApiError(401, "Invalid or missing token"));
    render(<AuditLog />);
    expect((await screen.findByRole("alert")).textContent).toBe("Invalid or missing token");
  });

  it("reloads on demand", async () => {
    audit.mockResolvedValueOnce(page([entry()])).mockResolvedValueOnce(page([entry(), entry()]));
    render(<AuditLog />);
    expect(await rows()).toHaveLength(1);

    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(currentRows()).toHaveLength(2));
  });
});
