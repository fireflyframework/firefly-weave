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
// The run log of a simulated run with transactions: one row per transaction
// after the step it followed, the undo steps under it in the order they ran,
// never under the steps they undo. The answers are the repository's own
// Studio host's.
import "@angular/compiler";
import { beforeAll, describe, expect, it, vi } from "vitest";
import { playbackFrames } from "../src/app/editor/canvas/trace-playback";
import {
  handledSentence,
  loopLog,
  runStatus,
  stoppedRow,
  type RunLogEntry,
} from "../src/app/editor/debugger/run-log-panel";
import { withTransactions } from "../src/app/editor/debugger/run-log-transactions";
import type { ExecuteResponse } from "../src/app/editor/state/execution";
import {
  simulatedRollback,
  type TransactionRun,
} from "../src/app/editor/transactions/rollback";
import { pythonAvailable } from "./python-path";
import { EXECUTIONS, executed } from "./transaction-answers";

const NAMES = [
  "undone",
  "stuck",
  "unknown",
  "confirmStuck",
  "completed",
  "definite",
  "continued",
  "inLoop",
  "loopStopped",
  "stopped",
  "nothingReserved",
  "loopInside",
  "nothingDone",
  "failsLater",
] as const;
type Name = (typeof NAMES)[number];
const hasPython = pythonAvailable();
let answers: Record<Name, ExecuteResponse>;
beforeAll(() => {
  if (hasPython)
    answers = executed(
      Object.fromEntries(NAMES.map((name) => [name, EXECUTIONS[name]])),
    ) as Record<Name, ExecuteResponse>;
}, 180_000);

const ROWS: Record<string, readonly string[]> = {
  move: ["each"],
  hold: ["each"],
  add: ["each"],
  submit: ["each"],
};
const around = (name: Name) => (id: string) =>
  name === "inLoop" || name === "loopStopped" ? (ROWS[id] ?? []) : [];
/** The rows as the run log lists them, the undo steps further in. */
function rows(name: Name, run?: TransactionRun, live = false) {
  const answer = answers[name];
  const steps = new Map<string, RunLogEntry>();
  for (const { nodeId, status, handled } of answer.trace)
    if (!around(name)(nodeId).length)
      steps.set(nodeId, {
        id: nodeId,
        status: handled ? "handled" : status,
        summary: null,
        stale: false,
        breakpoint: false,
      });
  return withTransactions(
    [...steps.values()],
    answer,
    run ?? simulatedRollback(answer, EXECUTIONS[name].workflow),
    live,
    around(name),
  );
}
const listed = (name: Name, run?: TransactionRun, live = false) =>
  rows(name, run, live).flatMap((row) => [
    `${row.id} ${row.status}`,
    ...(row.transactions ?? []).flatMap((one) => [
      `${one.title} · ${one.text}`,
      ...one.steps.map((step) => `  ${step.title} · ${step.text}`),
    ]),
  ]);

describe.skipIf(!hasPython)("the rows of a transaction", () => {
  it("test_the_run_log_lists_undo_steps_under_one_transaction_row_in_run_order", () => {
    expect(listed("undone")).toEqual([
      "hold completed",
      "add completed",
      "submit failed",
      "Transaction move · Undone",
      "  Undo of add · Undone",
      "  Release of hold · Released",
    ]);
    expect(listed("completed")).toEqual([
      "lookup completed",
      "hold completed",
      "add completed",
      "submit completed",
      "notify completed",
      "Transaction move · Completed",
      "  Confirm of hold · Confirmed",
      "summarize completed",
    ]);
    expect(listed("definite")).toEqual([
      "hold completed",
      "add failed",
      "Transaction move · Canceled",
      "  Release of hold · Released",
    ]);
  });

  it("says how often an undo step failed, and what is in doubt", () => {
    expect(listed("stuck").slice(2)).toEqual([
      "submit failed",
      "Transaction move · In doubt · action needed",
      "  Undo of add · Failed after 10 tries",
    ]);
    // Nothing was undone: it follows the step that failed.
    expect(listed("unknown").slice(2)).toEqual([
      "submit failed",
      "Transaction move · In doubt · action needed",
    ]);
    expect(listed("confirmStuck").slice(2)).toEqual([
      "submit completed",
      "Transaction move · In doubt · action needed",
      "  Confirm of hold · Failed after 10 tries",
    ]);
    const once = structuredClone(answers.stuck);
    once.transactions![0].participants[1].attempts = 1;
    const [, , submit] = withTransactions(
      rows("stuck").map(({ transactions: _, ...row }) => row),
      once,
      simulatedRollback(once, EXECUTIONS.stuck.workflow),
      false,
      () => [],
    );
    expect(submit.transactions![0].steps[0].text).toBe("Failed after 1 try");
  });

  it("names each row for a screen reader, by its icon's tone too", () => {
    const [, , submit] = rows("undone");
    expect(submit.transactions).toMatchObject([
      {
        key: "move",
        label: "Transaction move, undone",
        tone: "warning",
        icon: "undo",
        steps: [
          { key: "add", label: "Undo of add, undone", icon: "undo" },
          { key: "hold", label: "Release of hold, released", icon: "undo" },
        ],
      },
    ]);
    const stuck = rows("stuck")[2].transactions![0];
    expect(stuck).toMatchObject({
      label: "Transaction move, in doubt, action needed",
      tone: "danger",
      icon: "critical",
      steps: [
        {
          label: "Undo of add, failed after 10 tries",
          tone: "danger",
          icon: "critical",
        },
      ],
    });
    expect(rows("completed")[4].transactions).toMatchObject([
      {
        icon: "ok",
        tone: "success",
        steps: [
          {
            label: "Confirm of hold, confirmed",
            icon: "check",
            tone: "success",
          },
        ],
      },
    ]);
  });

  it("keeps a block that failed and continued, after what it undid", () => {
    expect(listed("continued")).toEqual([
      "hold completed",
      "add completed",
      "submit failed",
      "Transaction move · Undone",
      "  Undo of add · Undone",
      "  Release of hold · Released",
      "move handled",
      "summarize completed",
    ]);
  });

  it("follows the last step when the transaction itself failed", () => {
    const answer = structuredClone(answers.nothingDone);
    answer.transactions![0].cause!.instanceKey = "move";
    expect(
      withTransactions(
        rows("nothingDone").map(({ transactions: _, ...row }) => row),
        answer,
        simulatedRollback(answer, EXECUTIONS.nothingDone.workflow),
        false,
        () => [],
      ).map(({ id, transactions }) => [id, transactions?.[0].text]),
    ).toEqual([["check", "Canceled"]]);
  });

  it("has one row per item of a loop that undid anything, under the loop's", () => {
    for (const name of ["inLoop", "loopStopped"] as const) {
      expect(listed(name).slice(1), name).toEqual([
        "Transaction move, item 2 · Undone",
        "  Undo of add · Undone",
        "  Release of hold · Released",
      ]);
      const [one] = rows(name)[0].transactions!;
      // Each opens its step on that item.
      expect([one.key, ...one.steps.map(({ key }) => key)], name).toEqual([
        "move[1]",
        "add[1]",
        "hold[1]",
      ]);
    }
  });

  it("says which item each undo step is of, when a loop inside it made it run more than once", () => {
    expect(listed("loopInside").slice(-4)).toEqual([
      "Transaction move · Undone",
      "  Undo of add, item 3 · Undone",
      "  Undo of add, item 2 · Undone",
      "  Undo of add, item 1 · Undone",
    ]);
  });

  it("shows a transaction that finished without anything to confirm, none that was left open", () => {
    expect(listed("nothingReserved")).toEqual([
      "add completed",
      "submit completed",
      "Transaction move · Completed",
    ]);
    expect(listed("stopped")).toEqual(["hold completed", "add completed"]);
  });

  it("follows the playback: no row before its undo steps, then each as it plays", () => {
    const frames = playbackFrames(
      answers.undone,
      false,
      EXECUTIONS.undone.workflow,
    ).map(({ transactions, ended }) =>
      listed("undone", transactions, !ended).slice(3),
    );
    expect(frames.slice(0, 4)).toEqual([[], [], [], []]);
    expect(frames[4]).toEqual([
      "Transaction move · Undoing · 1 of 2",
      "  Undo of add · Running",
    ]);
    expect(frames[5]).toEqual([
      "Transaction move · Undoing · 2 of 2",
      "  Undo of add · Undone",
      "  Release of hold · Running",
    ]);
    expect(frames[6]).toHaveLength(3);
    const confirming = playbackFrames(
      answers.completed,
      false,
      EXECUTIONS.completed.workflow,
    ).find(({ current }, at) => at > 3 && current[0] === "hold")!;
    expect(
      listed("completed", confirming.transactions, true).slice(5, 7),
    ).toEqual(["Transaction move · Finishing", "  Confirm of hold · Running"]);
    // Not once the run is over, stopped half way.
    expect(listed("completed", confirming.transactions, false)).not.toContain(
      "Transaction move · Finishing",
    );
  });

  it("follows the step that failed when there was nothing to undo", () => {
    expect(listed("nothingDone")).toEqual([
      "check failed",
      "Transaction move · Canceled",
    ]);
  });
});

describe.skipIf(!hasPython)("the rest of the run log", () => {
  it("stops at the step that failed, with its own code", () => {
    for (const [name, step, code] of [
      ["undone", "submit", "REFUSED"],
      ["definite", "add", "VALIDATION"],
      ["unknown", "submit", "REFUSED"],
    ] as const)
      expect(
        stoppedRow(answers[name], EXECUTIONS[name].workflow),
        name,
      ).toMatchObject({ step, code });
    // Past one that continued, where it stopped later.
    expect(
      stoppedRow(answers.failsLater, EXECUTIONS.failsLater.workflow),
    ).toMatchObject({ step: "tell", code: "WV-TASK-RETRIES-EXHAUSTED" });
    // The run went on, or no step failed.
    for (const name of ["continued", "confirmStuck"] as const)
      expect(stoppedRow(answers[name], EXECUTIONS[name].workflow)).toBeNull();
  });

  it("says where the run ended in its header", () => {
    expect(runStatus(answers.undone, null)).toBe("Failed at submit");
    expect(runStatus(answers.loopStopped, null)).toBe(
      "Failed at submit, item 2",
    );
    expect(runStatus(answers.failsLater, null)).toBe("Failed at tell");
    expect(runStatus(answers.stuck, null)).toBe("Stopped · 2 steps");
    expect(runStatus(answers.confirmStuck, null)).toBe("Stopped · 3 steps");
    expect(runStatus(answers.continued, null)).toBe(
      "Succeeded with errors · 4 steps",
    );
    expect(handledSentence(answers.continued)).toBe(
      "The simulated run succeeded with 1 handled error.",
    );
  });

  it("reads a loop's item whose transaction was undone, and went on or stopped", () => {
    const [went] = loopLog(answers.inLoop, around("inLoop"));
    expect(
      went.items.map(({ status, text, code }) => [status, text, code]),
    ).toEqual([
      ["completed", "move, hold, add, submit", undefined],
      ["handled", "Failed, continued at move", "WV-TXN-ROLLED-BACK"],
      ["completed", "move, hold, add, submit", undefined],
    ]);
    const [stopped] = loopLog(
      answers.loopStopped,
      around("loopStopped"),
      EXECUTIONS.loopStopped.workflow,
    );
    // The transaction's own row says it was undone: no check for it here.
    expect(
      stopped.items[1].steps!.map(({ id, status, text }) => [id, status, text]),
    ).toEqual([
      ["hold", "completed", "1 field"],
      ["add", "completed", "1 field"],
      ["submit", "failed", "REFUSED"],
    ]);
    expect(stopped.items[1].text).toBe("Failed at submit");
  });
});
vi.setConfig({ testTimeout: 20_000 });
