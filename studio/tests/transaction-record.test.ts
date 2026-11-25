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
// What a transaction record says in Operate: each step's words, what stays
// applied when a transaction is left in doubt, what stays done in other
// systems, and the section's summary, folding, cause, timeline and resolution.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import type {
  ParticipantStatus,
  UndoMode,
} from "../src/app/editor/transactions/transaction-words";
import type {
  RunTransactionView,
  TransactionParticipantView,
} from "../src/app/operate/transactions/transaction-contracts";
import {
  RESOLUTION_LABELS,
  causeText,
  clockTime,
  foldedLine,
  recordBadge,
  recordRows,
  recordTimeline,
  resolutionText,
  retryDelay,
  sectionSummary,
  staysDone,
  type RecordInput,
} from "../src/app/operate/transactions/transaction-record";
import {
  completed,
  inDoubt,
  loop,
  loopRunId as R,
  reconciled,
  retried,
  undone,
} from "./run-transactions-fixture";

const corpus = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/transaction-corpus.json"),
    "utf8",
  ),
) as { resolutions: Record<string, { label: string }> };

const participant = (
  key: string,
  mode: UndoMode,
  status: ParticipantStatus,
  extra: Partial<TransactionParticipantView> = {},
): TransactionParticipantView => ({
  node_id: key.replace(/\[.*$/, ""),
  instance_key: key,
  mode,
  status,
  effect: "none",
  ...extra,
});
const record = (
  state: RecordInput["state"],
  ...participants: TransactionParticipantView[]
): RecordInput => ({ state, participants });
const row = (input: RecordInput, key: string) =>
  recordRows(input, R).find((item) => item.instanceKey === key)!;
const word = (text: string, icon: string | null, tone: string | null) => ({
  text,
  icon,
  tone,
});

describe("a transaction record's rows", () => {
  it("says how each step is undone, and what happened forward", () => {
    const rows = recordRows(
      record(
        "compensating",
        participant("hold", "reserve", "pending"),
        participant("add", "compensate", "dispatched"),
        participant("submit", "irreversible", "done"),
        participant("notify", "compensate", "failed"),
        participant("check", "irreversible", "unknown"),
        participant("skip", "compensate", "skipped"),
      ),
      R,
    );
    expect(rows.map((item) => [item.how, item.howIcon])).toEqual([
      ["Reserved", "lock"],
      ["Reversible", "undo"],
      ["Can't be undone", "revoke"],
      ["Reversible", "undo"],
      ["Can't be undone", "revoke"],
      ["Reversible", "undo"],
    ]);
    expect(rows.map((item) => item.forward)).toEqual([
      word("Not started", null, null),
      word("Running", "spinner", "info"),
      word("Done", "ok", "success"),
      word("Failed", "failCircle", "danger"),
      word("Unknown", "help", "warning"),
      word("Skipped", null, null),
    ]);
    expect(rows.every((item) => item.undo === null)).toBe(true);
  });

  it("words each undo, release and confirm phase, with its operation key", () => {
    const phases: [ParticipantStatus, UndoMode, string, string, string][] = [
      ["cancelling", "reserve", "Releasing", "spinner", "#cancel"],
      ["cancelled", "reserve", "Released", "ok", "#cancel"],
      ["cancel_stuck", "reserve", "Release failed", "failCircle", "#cancel"],
      ["undoing", "compensate", "Undoing", "spinner", "#undo"],
      ["undone", "compensate", "Undone", "ok", "#undo"],
      ["undo_stuck", "compensate", "Undo failed", "failCircle", "#undo"],
      ["confirming", "reserve", "Confirming", "spinner", "#confirm"],
      ["confirmed", "reserve", "Confirmed", "ok", "#confirm"],
      ["confirm_stuck", "reserve", "Confirm failed", "failCircle", "#confirm"],
    ];
    for (const [status, mode, text, icon, suffix] of phases) {
      const item = row(
        record("completed", participant("x", mode, status)),
        "x",
      );
      expect(item.undo?.text, status).toBe(text);
      expect(item.undo?.icon, status).toBe(icon);
      // The step went forward before its phase began.
      expect(item.forward, status).toEqual(word("Done", "ok", "success"));
      expect(item.operationKey, status).toBe(`${R}:x${suffix}`);
      expect(item.stuck, status).toBe(status.endsWith("_stuck"));
    }
    expect(
      row(record("open", participant("add", "compensate", "done")), "add")
        .operationKey,
    ).toBe(`${R}:add`);
  });

  it("says a failed step may have changed data while it is undone", () => {
    const item = row(
      record(
        "compensating",
        participant("add", "compensate", "undoing", { effect: "unknown" }),
      ),
      "add",
    );
    expect(item.forward).toEqual(
      word("May have changed data", "help", "warning"),
    );
  });

  it("counts tries only past the first, and names a called step's run and a pending step", () => {
    const input: RecordInput = {
      state: "in_doubt",
      participants: [
        participant("add", "compensate", "undo_stuck", {
          attempts: 3,
          child_run_id: "9a01c7d2-3e4f-4a5b-8c6d-7e8f9a0b1c2d",
        }),
        participant("hold", "reserve", "cancelled", { attempts: 1 }),
        participant("elsewhere", "compensate", "dispatched"),
      ],
      pending: ["elsewhere"],
    };
    const [add, hold, elsewhere] = recordRows(input, R);
    expect([add.attempts, hold.attempts, elsewhere.attempts]).toEqual([
      "3 tries",
      null,
      null,
    ]);
    expect(add.childRunId).toBe("9a01c7d2-3e4f-4a5b-8c6d-7e8f9a0b1c2d");
    expect(hold.childRunId).toBeNull();
    expect([add.pending, elsewhere.pending]).toEqual([false, true]);
    expect(add.stepId).toBe("add");
  });

  it("test_a_step_still_applied_in_doubt_loses_its_check", () => {
    for (const state of ["in_doubt", "force_ended"] as const) {
      const input = record(
        state,
        participant("hold", "reserve", "done"),
        participant("add", "compensate", "undo_stuck", { attempts: 3 }),
        participant("submit", "irreversible", "failed"),
      );
      const hold = row(input, "hold");
      expect(hold.forward, state).toEqual(word("Done", null, null));
      expect(hold.undo, state).toEqual(
        word("Still reserved", "lock", "warning"),
      );
      expect(hold.stillApplied, state).toBe(true);
      // A cell that already says what happened keeps its word.
      const add = row(input, "add");
      expect(add.undo, state).toEqual(
        word("Undo failed", "failCircle", "danger"),
      );
      expect(add.forward, state).toEqual(word("Done", null, null));
      // A failed step never ran, so nothing of it stays applied.
      expect(row(input, "submit").stillApplied, state).toBe(false);
      // The step that can't be undone is unknown: add stays done, not undone.
      const unknown = record(
        state,
        participant("hold", "reserve", "cancelled"),
        participant("add", "compensate", "done"),
        participant("submit", "irreversible", "unknown"),
      );
      expect(row(unknown, "add").undo, state).toEqual(
        word("Not undone", "warning", "warning"),
      );
      expect(row(unknown, "hold").stillApplied, state).toBe(false);
      // An undone step left nothing applied: it keeps its check and its word.
      const undoneStep = row(
        record(
          state,
          participant("add", "compensate", "undone"),
          participant("hold", "reserve", "cancel_stuck"),
        ),
        "add",
      );
      expect(undoneStep.stillApplied, state).toBe(false);
      expect(undoneStep.forward, state).toEqual(word("Done", "ok", "success"));
      expect(undoneStep.undo, state).toEqual(word("Undone", "ok", "warning"));
    }
  });

  it("says a status it doesn't know in plain words, never as if its step were done", () => {
    for (const state of ["in_doubt", "force_ended", "compensating"] as const)
      for (const mode of ["reserve", "compensate", "irreversible"] as const) {
        const newer = row(
          record(
            state,
            participant("add", mode, "held_open" as ParticipantStatus),
          ),
          "add",
        );
        expect(newer.forward, `${state} ${mode}`).toEqual(
          word("Held open", null, null),
        );
        expect(newer.undo, `${state} ${mode}`).toBeNull();
      }
  });

  it("test_a_transaction_that_went_ahead_keeps_done", () => {
    const confirmStuck = record(
      "in_doubt",
      participant("hold", "reserve", "confirm_stuck", { attempts: 10 }),
      participant("add", "compensate", "done"),
      participant("submit", "irreversible", "done"),
    );
    const afterStuck = record(
      "in_doubt",
      participant("hold", "reserve", "done"),
      participant("submit", "irreversible", "done"),
      participant("notify", "compensate", "failed"),
    );
    for (const input of [confirmStuck, afterStuck])
      for (const item of recordRows(input, R).filter(
        (each) => each.instanceKey !== "notify" && each.instanceKey !== "hold",
      )) {
        expect(item.forward).toEqual(word("Done", "ok", "success"));
        expect(item.undo).toBeNull();
        expect(item.stillApplied).toBe(false);
      }
    expect(row(afterStuck, "hold").undo).toBeNull();
    expect(row(confirmStuck, "hold").undo).toEqual(
      word("Confirm failed", "failCircle", "danger"),
    );
  });
});

describe("what stays done in other systems", () => {
  const line = (input: RecordInput) =>
    staysDone([input]).map(
      (item) => `${item.step} · ${item.how}: ${item.what}`,
    );

  it("says of a status it doesn't know that the step may or may not have happened, never that it stays done", () => {
    const newer = "held_open" as ParticipantStatus;
    expect(
      line(
        record(
          "in_doubt",
          participant("hold", "reserve", newer),
          participant("add", "compensate", newer),
          participant("submit", "irreversible", newer),
        ),
      ),
    ).toEqual([
      "hold · Reserved: it may or may not have happened",
      "add · Reversible: it may or may not have happened",
      "submit · Can't be undone: it may or may not have happened",
    ]);
  });

  it("lists each step still applied with what Weave won't do about it", () => {
    // Cancel without undoing a run while hold is reserved and add still runs.
    expect(
      line(
        record(
          "open",
          participant("hold", "reserve", "done"),
          participant("add", "compensate", "dispatched"),
          participant("submit", "irreversible", "pending"),
        ),
      ),
    ).toEqual([
      "hold · Reserved: Weave won't release it",
      "add · Reversible: still running and may finish; Weave won't undo it",
    ]);
    // Force end while the step that can't be undone is unknown.
    expect(
      line(
        record(
          "in_doubt",
          participant("hold", "reserve", "confirmed"),
          participant("add", "compensate", "done"),
          participant("submit", "irreversible", "unknown"),
        ),
      ),
    ).toEqual([
      "hold · Reserved: Weave won't release it",
      "add · Reversible: Weave won't undo it",
      "submit · Can't be undone: it may or may not have happened",
    ]);
  });

  it("words a running reserved or irreversible step, a failed undo and a done irreversible step", () => {
    expect(
      line(
        record(
          "deciding",
          participant("hold", "reserve", "dispatched"),
          participant("add", "compensate", "undo_stuck"),
          participant("submit", "irreversible", "dispatched"),
          participant("notify", "irreversible", "done"),
        ),
      ),
    ).toEqual([
      "hold · Reserved: still running and may finish; Weave won't release it",
      "add · Reversible: its undo failed, and Weave won't try again",
      "submit · Can't be undone: still running and may finish",
      "notify · Can't be undone: it stays done",
    ]);
  });

  it("leaves out what was released, undone, failed or never started", () => {
    expect(
      line(
        record(
          "compensating",
          participant("hold", "reserve", "cancelled"),
          participant("add", "compensate", "undone"),
          participant("submit", "irreversible", "failed"),
          participant("notify", "compensate", "pending"),
          participant("skip", "compensate", "skipped"),
        ),
      ),
    ).toEqual([]);
    // A held run's record has no steps: it adds nothing.
    expect(staysDone([record("in_doubt")])).toEqual([]);
    // Several records read one after another; steps keep their loop index.
    expect(staysDone([inDoubt, completed]).map((item) => item.step)).toEqual([
      "hold[3]",
      "add[3]",
      "hold[1]",
      "add[1]",
      "submit[1]",
    ]);
  });
});

describe("the Transactions section", () => {
  it("sums up the run's transactions, the one in doubt first", () => {
    expect(sectionSummary(loop)).toEqual({
      text: "5 transactions: 1 in doubt, 1 undone, 1 reconciled, 2 completed",
      firstInDoubt: "move[3]",
    });
    expect(sectionSummary([completed])).toEqual({
      text: "1 transaction: 1 completed",
      firstInDoubt: null,
    });
    expect(
      sectionSummary([
        { ...completed, state: "force_ended" },
        { ...completed, state: "compensating" },
        { ...completed, state: "open" },
        { ...completed, state: "cancelled" },
        { ...completed, state: "deciding" },
        { ...completed, state: "committing" },
      ]).text,
    ).toBe(
      "6 transactions: 1 force ended, 1 undoing, 1 deciding, 1 finishing, 1 open, 1 canceled",
    );
  });

  it("folds a completed transaction nobody had to resolve to one line", () => {
    expect(foldedLine(completed, "UTC")).toEqual([
      "Business key T-1050",
      "3 steps",
      "Completed at 10:31:05",
    ]);
    const { business_key: _, ...keyless } = completed;
    expect(
      foldedLine(
        { ...keyless, participants: [completed.participants[0]] },
        "UTC",
      ),
    ).toEqual(["1 step", "Completed at 10:31:05"]);
    for (const view of [retried, undone, inDoubt, reconciled])
      expect(foldedLine(view, "UTC"), view.instance_key).toBeNull();
    expect(
      foldedLine({ ...completed, incident_id: inDoubt.incident_id }, "UTC"),
    ).toBeNull();
  });

  it("sums up the first page the same way while more follow", () => {
    expect(sectionSummary(loop, true)).toEqual({
      text: "The first 5 of this run's transactions: 1 in doubt, 1 undone, 1 reconciled, 2 completed",
      firstInDoubt: "move[3]",
    });
  });

  it("says no more of a transaction's undo than its state does", () => {
    const states = [
      "compensating",
      "compensated",
      "cancelled",
      "reconciled",
      "force_ended",
      // Its undo began, whatever it came to since.
      "completed",
    ] as const;
    expect(
      states.map((state) => [state, causeText({ ...undone, state })]),
    ).toEqual([
      ["compensating", "Undoing after add[2] failed: UNAVAILABLE"],
      ["compensated", "Undone after add[2] failed: UNAVAILABLE"],
      ["cancelled", "Undo started after add[2] failed: UNAVAILABLE"],
      ["reconciled", "Undo started after add[2] failed: UNAVAILABLE"],
      ["force_ended", "Undo started after add[2] failed: UNAVAILABLE"],
      ["completed", "Undo started after add[2] failed: UNAVAILABLE"],
    ]);
  });

  it("says why a transaction left its path: the failed step, or what is in doubt", () => {
    expect(causeText(reconciled)).toBe(
      "Undo started after submit[4] failed: REJECTED",
    );
    // In doubt: the stuck step says why, whatever failed first.
    expect(causeText(inDoubt)).toBe(
      "In doubt: A step's undo couldn't finish, so the transaction is in doubt.",
    );
    const inDoubtWith = (...participants: TransactionParticipantView[]) =>
      causeText({ ...inDoubt, participants });
    expect(
      inDoubtWith(
        participant("hold", "reserve", "cancel_stuck"),
        participant("add", "compensate", "undo_stuck"),
      ),
    ).toBe(
      "In doubt: A reservation couldn't be released, so the transaction is in doubt.",
    );
    expect(inDoubtWith(participant("hold", "reserve", "confirm_stuck"))).toBe(
      "In doubt: A reservation couldn't be confirmed after the transaction went ahead.",
    );
    expect(
      inDoubtWith(
        participant("add", "compensate", "done"),
        participant("submit", "irreversible", "unknown"),
      ),
    ).toBe(
      "In doubt: The step that can't be undone may or may not have happened.",
    );
    expect(
      inDoubtWith(
        participant("submit", "irreversible", "done"),
        participant("notify", "compensate", "failed"),
      ),
    ).toBe("In doubt: A step after the point of no return couldn't finish.");
    expect(
      inDoubtWith(
        participant("hold", "reserve", "done"),
        participant("elsewhere", "compensate", "dispatched"),
      ),
    ).toBe(
      "In doubt: Steps still running didn't finish in time, so nothing can be undone safely yet.",
    );
    // A cause of the platform's own reads its sentence.
    expect(
      causeText({
        ...undone,
        cause: {
          code: "WV-TXN-RUN-CANCELLED",
          instance_key: "move[2]",
          effect: "none",
        },
      }),
    ).toBe("The run was canceled, so the transaction was undone.");
    // Nothing was undone, so the failure isn't a reason to undo.
    expect(
      causeText({
        ...completed,
        cause: {
          code: "TIMEOUT",
          instance_key: "submit[1]",
          effect: "unknown",
        },
      }),
    ).toBeNull();
    expect(causeText(completed)).toBeNull();
  });

  it("lists the record's moments in the order they happened", () => {
    const at = (view: RunTransactionView) =>
      recordTimeline(view).map(({ at: time, text }) => `${time} ${text}`);
    expect(at(retried)).toEqual([
      "2026-11-22T10:31:02Z Opened",
      "2026-11-22T10:31:04Z Decided to finish",
      "2026-11-22T10:40:00Z Resolved: Retry the confirm",
      "2026-11-22T10:40:03Z Completed",
    ]);
    // Ended and resolved at the same moment: the end word comes first.
    expect(at(reconciled)).toEqual([
      "2026-11-22T10:31:02Z Opened",
      "2026-11-22T10:48:17Z Reconciled",
      "2026-11-22T10:48:17Z Resolved: Mark as reconciled as undone",
    ]);
    expect(at(inDoubt)).toEqual(["2026-11-22T10:31:02Z Opened"]);
  });

  it("names each resolution as the action the person chose", () => {
    const resolution = (
      kind: NonNullable<RunTransactionView["resolution"]>["kind"],
      reconciled_as?: "completed" | "compensated",
    ) => ({
      kind,
      reason: "Checked",
      ...(reconciled_as ? { reconciled_as } : {}),
    });
    expect(resolutionText(resolution("terminate"))).toBe("Force end");
    expect(resolutionText(resolution("accept_reconciled_failure"))).toBe(
      "It didn't happen",
    );
    expect(resolutionText(resolution("accept_reconciled_result"))).toBe(
      "It happened",
    );
    expect(
      resolutionText(resolution("accept_reconciled_result", "compensated")),
    ).toBe("Mark as reconciled as undone");
    expect(
      resolutionText(resolution("accept_reconciled_result", "completed")),
    ).toBe("Mark as reconciled as finished");
    // A retry is named by the phase the record shows it retried.
    const retryOf = (...participants: TransactionParticipantView[]) =>
      resolutionText(resolution("retry_safe"), participants);
    expect(retryOf(participant("add", "compensate", "undo_stuck"))).toBe(
      "Retry the undo",
    );
    expect(
      retryOf(participant("add", "compensate", "undone", { attempts: 4 })),
    ).toBe("Retry the undo");
    // One try is no retry, and Wait again leaves no more: the record can't
    // tell which it was.
    expect(
      retryOf(participant("add", "compensate", "undone", { attempts: 1 })),
    ).toBe("Retry or Wait again");
    expect(
      retryOf(participant("hold", "reserve", "cancelled", { attempts: 11 })),
    ).toBe("Retry the release");
    expect(retryOf(...retried.participants)).toBe("Retry the confirm");
    expect(retryOf(participant("submit", "irreversible", "done"))).toBe(
      "Retry or Wait again",
    );
    expect(RESOLUTION_LABELS).toEqual(
      Object.fromEntries(
        Object.entries(corpus.resolutions).map(([action, { label }]) => [
          action,
          label,
        ]),
      ),
    );
  });

  it("words the pill as the badge does, counting undone steps and naming the step it waits for", () => {
    expect(recordBadge(inDoubt)).toEqual({
      text: "In doubt · action needed",
      tone: "danger",
    });
    expect(
      recordBadge({
        ...undone,
        state: "compensating",
        participants: [
          participant("hold", "reserve", "cancelling"),
          participant("add", "compensate", "undone"),
          participant("submit", "irreversible", "failed"),
        ],
      }),
    ).toEqual({ text: "Undoing · 1 of 2", tone: "info" });
    expect(
      recordBadge({
        ...completed,
        state: "deciding",
        participants: [
          participant("hold", "reserve", "done"),
          participant("submit", "irreversible", "dispatched"),
        ],
      }),
    ).toEqual({
      text: "Waiting for submit, which can't be undone",
      tone: "info",
    });
    // An open transaction has no badge words: its pill still says its state.
    expect(recordBadge({ ...completed, state: "open" })).toEqual({
      text: "Open",
      tone: "neutral",
    });
    expect(recordBadge(reconciled)).toEqual({
      text: "Reconciled",
      tone: "neutral",
    });
  });

  it("waits 5 s after a failed read, twice as long after each next one, at most 60 s", () => {
    expect([1, 2, 3, 4, 5, 6].map(retryDelay)).toEqual([
      5_000, 10_000, 20_000, 40_000, 60_000, 60_000,
    ]);
  });

  it("writes times of day on a 24-hour clock", () => {
    expect(clockTime("2026-11-22T10:31:05Z", "UTC")).toBe("10:31:05");
    expect(clockTime("2026-11-22T22:01:00Z", "UTC")).toBe("22:01:00");
    expect(clockTime("not a time", "UTC")).toBe("");
  });
});
