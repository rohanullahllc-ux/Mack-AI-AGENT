import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { RunBlock } from "../src/components/RunBlock";
import type { RunView, Step } from "../src/lib/types";

const run = (steps: Step[], over: Partial<RunView> = {}): RunView => ({
  key: "k",
  id: "r1",
  userText: "hello there",
  status: "completed",
  steps,
  costUsd: 0,
  modelSteps: 0,
  inputTokens: 0,
  outputTokens: 0,
  lastSeq: 0,
  ...over,
});

const pending = (over: Partial<Extract<Step, { kind: "tool" }>> = {}): Step => ({
  kind: "tool",
  id: "tu1",
  name: "send_email",
  input: { to: "a@b.c" },
  source: "client",
  tier: "write",
  status: "awaiting_approval",
  ...over,
});

describe("RunBlock approvals", () => {
  it("sends the user's answer for the pending tool call", async () => {
    const onDecide = vi.fn().mockResolvedValue(undefined);
    render(<RunBlock run={run([pending()], { status: "running" })} onDecide={onDecide} />);

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(onDecide).toHaveBeenCalledWith("tu1", true);
  });

  it("shows the error and lets the user retry when the answer fails to send", async () => {
    const onDecide = vi.fn().mockRejectedValueOnce(new Error("server down")).mockResolvedValue(undefined);
    render(<RunBlock run={run([pending()], { status: "running" })} onDecide={onDecide} />);

    await userEvent.click(screen.getByRole("button", { name: "Deny" }));
    expect((await screen.findByRole("alert")).textContent).toBe("server down");

    await userEvent.click(screen.getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(onDecide).toHaveBeenCalledTimes(2));
    expect(onDecide).toHaveBeenLastCalledWith("tu1", false);
  });

  it("is read-only without onDecide", () => {
    render(<RunBlock run={run([pending()], { status: "running" })} />);
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.getByText(/Waiting for approval/)).toBeTruthy();
  });

  it("states the auto-deny timeout", () => {
    render(<RunBlock run={run([pending({ approvalTimeoutS: 300 })])} onDecide={vi.fn()} />);
    expect(screen.getByText(/Denied automatically after 5 min/)).toBeTruthy();
  });
});

describe("RunBlock summary", () => {
  it("shows the user message and totals once finished", () => {
    render(<RunBlock run={run([], { modelSteps: 2, costUsd: 0.0123, inputTokens: 1000, outputTokens: 234 })} />);
    expect(screen.getByText("hello there")).toBeTruthy();
    expect(screen.getByText("2 model calls")).toBeTruthy();
    expect(screen.getByText("$0.012")).toBeTruthy();
    expect(screen.getByText("1,234 tokens")).toBeTruthy();
  });

  it("hides the user message when showUser is off", () => {
    render(<RunBlock run={run([])} showUser={false} />);
    expect(screen.queryByText("hello there")).toBeNull();
  });
});
