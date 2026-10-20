/*
Copyright 2026 Firefly Software Foundation.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
Author: Firefly Software Foundation
SPDX-License-Identifier: Apache-2.0
*/
import { describe, expect, it } from "vitest";
import {
  FOUR_EYES_COPY,
  approvalToast,
  approveBy,
  blockedByFourEyes,
  changeActions,
  changeCountdown,
  effectiveState,
  planLifetimeError,
  progressSteps,
  replannable,
  wizardSteps,
} from "../src/app/operate/clusters/change-request-model";
import { at, change, serverNow } from "./change-request-fixture";

// The browser's clock runs 7 s ahead of the server's; Studio counts on the server's.
const offset = -7000;
const browserNow = serverNow - offset;

describe("the approval countdown", () => {
  it("is neutral above 2 minutes, warns at 2 minutes, is urgent at 30 seconds, then expires", () => {
    const tone = (seconds: number) =>
      changeCountdown(
        change({ plan_expires_at: at(seconds) }),
        browserNow + offset,
      );
    expect(tone(121)).toMatchObject({
      tone: "neutral",
      label: "2:01",
      note: "",
    });
    expect(tone(120)).toMatchObject({
      tone: "warning",
      label: "2:00",
      note: "Expires soon",
    });
    expect(tone(31)).toMatchObject({ tone: "warning", label: "0:31" });
    expect(tone(30)).toMatchObject({ tone: "danger", label: "0:30" });
    expect(tone(0)).toMatchObject({ tone: "expired", label: "Expired" });
  });
  it("counts a draining change down to when its drain must start", () => {
    const draining = change({
      drain: { mode: "drain_first", timeout_seconds: 300 },
      plan_expires_at: at(670),
    });
    expect(approveBy(draining)).toBe(serverNow + 250_000);
    expect(changeCountdown(draining, serverNow)).toMatchObject({
      label: "4:10",
      note: "Approve within 4:10",
    });
    expect(approveBy(change({ plan_expires_at: at(600) }))).toBe(
      serverNow + 600_000,
    );
    expect(approveBy(change({ plan_expires_at: null }))).toBeNull();
  });
});

describe("states and lifetimes", () => {
  it("reads a planned change past its expiry as expired, like the server", () => {
    expect(effectiveState(change({ plan_expires_at: at(0) }), serverNow)).toBe(
      "expired",
    );
    expect(effectiveState(change({ plan_expires_at: at(1) }), serverNow)).toBe(
      "awaiting_approval",
    );
    expect(
      effectiveState(
        change({ state: "applying", plan_expires_at: at(-5) }),
        serverNow,
      ),
    ).toBe("applying");
  });
  it("needs a plan that outlives the drain by 3 minutes", () => {
    expect(planLifetimeError(true, 300, 480)).toBeNull();
    expect(planLifetimeError(true, 300, 479)).toBe(
      "The plan must live at least 8 minutes to let the workers finish and apply.",
    );
    expect(planLifetimeError(false, 300, 60)).toBeNull();
  });
  it("names the wizard's steps", () => {
    expect(wizardSteps).toEqual(["Change", "Details", "Review", "Progress"]);
  });
  it("shows progress, skipping the drain for a change that does not drain", () => {
    const steps = progressSteps(change(), serverNow);
    expect(steps.map((step) => [step.label, step.state])).toEqual([
      ["Checking the target", "done"],
      ["Plan ready", "current"],
      ["Approved", "todo"],
      ["Letting workers finish", "skipped"],
      ["Applying and checking", "todo"],
      ["Done", "todo"],
    ]);
    const failed = progressSteps(
      change({
        state: "failed",
        failure: {
          code: "WV-PROVIDER-FAILED",
          message: "The runner could not apply the change",
        },
      }),
      serverNow,
    );
    expect(failed.at(-1)).toEqual({
      label: "Failed",
      state: "failed",
      detail: "The runner could not apply the change",
    });
  });
});

describe("what the viewer may do", () => {
  const all = () => true;
  const only =
    (...held: string[]) =>
    (capability: string) =>
      held.includes(capability);
  it("blocks the requester and the planner from approving while a second person is needed", () => {
    expect(changeActions(change(), all, "ana", serverNow).approve).toBe(
      "blocked",
    );
    expect(
      changeActions(change({ planned_by: "bo" }), all, "bo", serverNow).approve,
    ).toBe("blocked");
    expect(changeActions(change(), all, "carmen", serverNow).approve).toBe(
      "enabled",
    );
    expect(
      changeActions(change(), only("deployment.read"), "carmen", serverNow)
        .approve,
    ).toBe("hidden");
    const relaxed = change({ approval_policy: { distinct_approver: false } });
    expect(blockedByFourEyes(relaxed, "ana", false)).toBe(false);
    // The stricter of the change's snapshot and the target's policy applies.
    expect(blockedByFourEyes(relaxed, "ana", true)).toBe(true);
    expect(FOUR_EYES_COPY).toBe(
      "You requested or planned this change. Another approver must review it.",
    );
  });
  it("offers draining, applying and forcing only when they can work", () => {
    const approved = change({
      state: "approved",
      drain: { mode: "drain_first", timeout_seconds: 300 },
      plan_expires_at: at(800),
      drain_set: [{ worker_id: "w1", host: "c0ffee000003", active_leases: 1 }],
    });
    const actions = changeActions(approved, all, "carmen", serverNow);
    expect(actions).toMatchObject({
      drain: true,
      apply: true,
      force: false,
      cancel: true,
      decline: true,
    });
    // Past the drain window the drain cannot start; planning again can.
    const late = { ...approved, plan_expires_at: at(400) };
    expect(changeActions(late, all, "carmen", serverNow)).toMatchObject({
      drain: false,
      replan: true,
    });
    const draining = {
      ...approved,
      state: "draining" as const,
      history: [
        ...approved.history,
        { at: at(-301), state: "draining" as const, actor_id: "carmen" },
      ],
    };
    expect(changeActions(draining, all, "carmen", serverNow).force).toBe(true);
    expect(
      changeActions(draining, only("deployment.apply"), "carmen", serverNow)
        .force,
    ).toBe(false);
    expect(
      changeActions(change({ state: "applying" }), all, "carmen", serverNow),
    ).toMatchObject({
      cancel: false,
      apply: false,
      approve: "hidden",
    });
  });
  it("plans again only what planning again can fix", () => {
    expect(replannable(change({ state: "superseded" }), serverNow)).toBe(true);
    expect(replannable(change({ plan_expires_at: at(-1) }), serverNow)).toBe(
      true,
    );
    expect(
      replannable(
        change({
          state: "failed",
          failure: { code: "WV-PROVIDER-FAILED", message: "" },
        }),
        serverNow,
      ),
    ).toBe(false);
    expect(
      replannable(
        change({
          state: "failed",
          failure: { code: "WV-DRAIN-WINDOW", message: "" },
        }),
        serverNow,
      ),
    ).toBe(true);
  });
});

describe("the approval notice", () => {
  it("announces the first new change waiting for approval, once", () => {
    const first = change({ plan_expires_at: at(582) });
    expect(approvalToast(new Set(), [first], serverNow)).toBe(
      "Plan ready for approval: Scale fixture-worker to 3 — expires in 9:42",
    );
    expect(approvalToast(new Set([first.id]), [first], serverNow)).toBeNull();
  });
});

describe("a change that needs a check", () => {
  it("says the workers it let finish take new tasks again", () => {
    const steps = progressSteps(
      change({ state: "reconciliation_required" }),
      serverNow,
    );
    const last = steps[steps.length - 1];
    expect(last.label).toBe("Needs a check");
    expect(last.detail).toContain("take new tasks again");
  });
});

describe("making the plan and the main action", () => {
  const all = () => true;
  it("offers Plan this change on a checked change to someone who may plan", () => {
    const checked = change({
      state: "ready_to_plan",
      plan_id: null,
      plan_digest: null,
      plan_expires_at: null,
    });
    expect(changeActions(checked, all, "carmen", serverNow).plan).toBe(true);
    expect(
      changeActions(
        checked,
        (c) => c === "deployment.read",
        "carmen",
        serverNow,
      ).plan,
    ).toBe(false);
    expect(changeActions(change(), all, "carmen", serverNow).plan).toBe(false);
  });
  it("makes Apply the main action only once the workers are finishing, or when none must", () => {
    const drainSet = [{ worker_id: "w1", host: "h1", active_leases: 1 }];
    const approved = change({
      state: "approved",
      drain: { mode: "drain_first", timeout_seconds: 300 },
      drain_set: drainSet,
      plan_expires_at: at(840),
    });
    expect(changeActions(approved, all, "carmen", serverNow).applyFirst).toBe(
      false,
    );
    expect(
      changeActions(
        change({ ...approved, state: "draining" }),
        all,
        "carmen",
        serverNow,
      ).applyFirst,
    ).toBe(true);
    expect(
      changeActions(
        change({ state: "approved", drain_set: [] }),
        all,
        "carmen",
        serverNow,
      ).applyFirst,
    ).toBe(true);
  });
  it("asks for a reason to apply when some workers match no registered worker", () => {
    const unmatched = change({
      state: "approved",
      drain_set: [],
      warnings: ["WV-DRAIN-UNKNOWN"],
    });
    expect(changeActions(unmatched, all, "carmen", serverNow)).toMatchObject({
      apply: true,
      reasonToApply: true,
    });
    // Removing them without a drain is forced, which needs worker.drain.
    expect(
      changeActions(unmatched, (c) => c !== "worker.drain", "carmen", serverNow)
        .apply,
    ).toBe(false);
    expect(
      changeActions(
        change({ state: "approved", drain_set: [] }),
        all,
        "carmen",
        serverNow,
      ).reasonToApply,
    ).toBe(false);
  });
});
