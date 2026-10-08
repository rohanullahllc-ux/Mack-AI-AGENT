import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ActivityPage from "../src/app/activity/page";
import { api } from "../src/lib/api";

vi.mock("../src/components/Shell", () => ({ useApp: () => ({ config: null }) }));
vi.mock("../src/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../src/lib/api")>()),
  api: { runs: vi.fn(), run: vi.fn(), audit: vi.fn() },
}));

const selected = () => screen.getAllByRole("tab").find((t) => t.getAttribute("aria-selected") === "true")?.textContent;

beforeEach(() => {
  vi.mocked(api.runs).mockReset().mockResolvedValue([]);
  vi.mocked(api.audit).mockReset().mockResolvedValue({ items: [], total: 0, counts: {} });
  window.history.replaceState(null, "", "/activity");
});

describe("Activity tabs", () => {
  it("opens on Runs and only loads the audit log when it is asked for", async () => {
    render(<ActivityPage />);
    expect(selected()).toBe("Runs");
    expect(await screen.findByText(/No runs yet/)).toBeTruthy();
    expect(api.audit).not.toHaveBeenCalled();
  });

  it("switches to the audit log and back, keeping the choice in the URL", async () => {
    render(<ActivityPage />);
    await userEvent.click(screen.getByRole("tab", { name: "Audit log" }));
    expect(selected()).toBe("Audit log");
    expect(window.location.search).toBe("?tab=audit");
    expect(await screen.findByText(/Nothing recorded yet/)).toBeTruthy();
    expect(screen.queryByText("Spent this month")).toBeNull(); // the run stats belong to the other tab

    await userEvent.click(screen.getByRole("tab", { name: "Runs" }));
    await waitFor(() => expect(selected()).toBe("Runs"));
    expect(window.location.search).toBe("");
    expect(screen.getByText("Spent this month")).toBeTruthy();
  });

  it("opens straight onto the audit log from a link", async () => {
    window.history.replaceState(null, "", "/activity?tab=audit");
    render(<ActivityPage />);
    await waitFor(() => expect(selected()).toBe("Audit log"));
    expect(await screen.findByText(/Nothing recorded yet/)).toBeTruthy();
  });

  it("ignores a tab name it does not know", () => {
    window.history.replaceState(null, "", "/activity?tab=bogus");
    render(<ActivityPage />);
    expect(selected()).toBe("Runs");
  });
});
