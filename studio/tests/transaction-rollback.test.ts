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
// What a simulated run did to its transactions, as the canvas shows it: the
// frame's state, each step's word and badge, the undo steps played back in
// the order they ran, and the edges they follow. The answers are the
// repository's own Studio host's.
import { beforeAll, describe, expect, it, vi } from "vitest";
import {
  edgeRun,
  groupsEntered,
  tileRun,
  type CanvasRun,
} from "../src/app/editor/canvas/run-state";
import {
  tileBorders,
  type TileFacts,
} from "../src/app/editor/canvas/tile-facts";
import {
  stepBadge,
  transactionTileRun,
} from "../src/app/editor/canvas/transaction-tile";
import {
  finalAnnouncement,
  finalRun,
  playbackFrames,
  runFailureOf,
} from "../src/app/editor/canvas/trace-playback";
import type {
  ExecuteResponse,
  TransactionTrace,
} from "../src/app/editor/state/execution";
import { transactionSummary } from "../src/app/editor/state/run-summary";
import {
  frameState,
  playedBack,
  simulatedRollback,
  stepState,
} from "../src/app/editor/transactions/rollback";
import {
  stateIcon,
  stateText,
} from "../src/app/editor/canvas/transaction-frame";
import { stateBadge } from "../src/app/editor/transactions/transaction-words";
import { pythonAvailable } from "./python-path";
import { EXECUTIONS, executed } from "./transaction-answers";

type Name = keyof typeof EXECUTIONS;
const hasPython = pythonAvailable();
let answers: Record<Name, ExecuteResponse>;
beforeAll(() => {
  if (hasPython) answers = executed(EXECUTIONS);
}, 180_000);

const rollback = (name: Name) =>
  simulatedRollback(answers[name], EXECUTIONS[name].workflow);
const word = (name: Name, id: string) => {
  const state = stepState(rollback(name), id, false);
  return state?.text ? `${state.text}/${state.tone}` : null;
};
const frame = (name: Name, id = "move") => {
  const state = frameState(rollback(name), id, false);
  return state ? `${state.text}${state.item}/${state.tone}` : null;
};
const frames = (name: Name, reduced = false) =>
  playbackFrames(answers[name], reduced, EXECUTIONS[name].workflow);
/** One moment of the playback: where it is, what the frame and some steps say. */
const moment = (run: CanvasRun, ...ids: string[]) => {
  const live = !run.ended;
  const state = run.transactions && frameState(run.transactions, "move", live);
  return [
    run.current.join(),
    state ? state.text : null,
    ...ids.map((id) =>
      run.transactions
        ? (stepState(run.transactions, id, live)?.text ?? null)
        : null,
    ),
  ];
};
/** A step's tile, as the canvas draws it at a moment of a run. */
function tile(run: CanvasRun, id: string, failure: TileFacts["failure"] = {}) {
  const state = run.transactions && stepState(run.transactions, id, !run.ended);
  const facts: TileFacts = {
    errors: [],
    warnings: [],
    setup: [],
    run: transactionTileRun(tileRun(id, run), state),
    failure,
    stale: null,
    pinned: false,
    selected: false,
    simulated: true,
  };
  return { badge: stepBadge(facts, state), borders: tileBorders(facts) };
}

describe.skipIf(!hasPython)("what a transaction came to", () => {
  it("reads each step and the frame in the platform's words", () => {
    const read = (name: Name) => [
      frame(name),
      ...["hold", "add", "submit"].map((id) => word(name, id)),
    ];
    expect(read("undone")).toEqual([
      "Undone/warning",
      "Released/warning",
      "Undone/warning",
      "Failed/danger",
    ]);
    expect(read("stuck")).toEqual([
      "In doubt · action needed/danger",
      "Still reserved/warning",
      "Undo failed/danger",
      "Failed/danger",
    ]);
    expect(read("unknown")).toEqual([
      "In doubt · action needed/danger",
      "Still reserved/warning",
      "Not undone/warning",
      "Unknown/warning",
    ]);
    expect(read("completed")).toEqual([
      "Completed/success",
      "Confirmed/success",
      "Done/success",
      "Done/success",
    ]);
    // A failure that changed nothing has nothing to undo.
    expect(read("definite")).toEqual([
      "Canceled/warning",
      "Released/warning",
      "Failed/danger",
      null,
    ]);
    expect(read("continued")).toEqual([
      "Undone/warning",
      "Released/warning",
      "Undone/warning",
      "Failed/danger",
    ]);
  });

  it("test_a_transaction_that_went_ahead_keeps_done_in_doubt", () => {
    const went = (name: Name) => rollback(name).byFrame.get("move")?.wentAhead;
    expect(went("confirmStuck")).toBe(true);
    expect(went("stuck")).toBe(false);
    expect(went("unknown")).toBe(false);
    expect([
      frame("confirmStuck"),
      ...["hold", "add", "submit"].map((id) => word("confirmStuck", id)),
    ]).toEqual([
      "In doubt · action needed/danger",
      "Confirm failed/danger",
      "Done/success",
      "Done/success",
    ]);
    const run = finalRun(
      answers.confirmStuck,
      EXECUTIONS.confirmStuck.workflow,
    );
    for (const id of ["add", "submit"])
      expect(tile(run, id).badge?.kind, id).toBe("done");
    expect(tile(run, "hold")).toEqual({
      badge: {
        kind: "stuck",
        tone: "danger",
        icons: ["critical"],
        text: "Confirm failed",
      },
      borders: { selected: false, live: false, error: true },
    });
  });

  it("lists the undo steps in the order they ran, with the tries of one that failed", () => {
    const rows = (name: Name) =>
      rollback(name).phases.map(({ text, status, stepId, instanceKey }) => [
        text,
        status,
        stepId,
        instanceKey,
      ]);
    expect(rows("undone")).toEqual([
      ["Undo of add", "completed", "add", "add#undo"],
      ["Release of hold", "completed", "hold", "hold#cancel"],
    ]);
    expect(rows("stuck")).toEqual([
      ["Undo of add (try 10 of 10)", "failed", "add", "add#undo"],
    ]);
    expect(rows("definite")).toEqual([
      ["Release of hold", "completed", "hold", "hold#cancel"],
    ]);
    expect(rows("completed")).toEqual([
      ["Confirm of hold", "completed", "hold", "hold#confirm"],
    ]);
    expect(rows("unknown")).toEqual([]);
  });

  it("names the incident of one in doubt", () => {
    expect(rollback("stuck").byFrame.get("move")?.incidentKey).toBe(
      "add#undo:10",
    );
    expect(rollback("undone").byFrame.get("move")?.incidentKey).toBeUndefined();
  });

  it("counts the tries against the transaction's own Max tries", () => {
    const own = structuredClone(EXECUTIONS.stuck.workflow);
    own.spec.steps[0]["policy"] = { undoRetry: { maxAttempts: 12 } };
    expect(simulatedRollback(answers.stuck, own).phases[0].text).toBe(
      "Undo of add (try 10 of 12)",
    );
  });

  it("keeps one transaction per item of a loop, and shows the item that was undone", () => {
    const run = rollback("inLoop");
    expect([...run.byFrame].map(([key, { state }]) => [key, state])).toEqual([
      ["move[0]", "completed"],
      ["move[1]", "compensated"],
      ["move[2]", "completed"],
    ]);
    expect(run.byStep.get("add[1]")).toBe("undone");
    expect(run.byStep.get("add[2]")).toBe("done");
    expect(frame("inLoop")).toBe("Undone, item 2/warning");
    expect(word("inLoop", "hold")).toBe("Released/warning");
    expect(word("inLoop", "submit")).toBe("Failed/danger");
    // One state for all: no item to name.
    const all = structuredClone(answers.inLoop);
    all.transactions!.splice(1, 1);
    expect(
      frameState(
        simulatedRollback(all, EXECUTIONS.inLoop.workflow),
        "move",
        false,
      ),
    ).toMatchObject({ text: "Completed", item: "" });
  });

  it("shows the item that needs attention most, the first of them", () => {
    const { workflow } = EXECUTIONS.inLoop;
    const ended = (...states: TransactionTrace["state"][]) => {
      const answer = structuredClone(answers.inLoop);
      answer.transactions!.forEach((one, at) => (one.state = states[at]));
      const state = frameState(
        simulatedRollback(answer, workflow),
        "move",
        false,
      );
      return state && state.text + state.item;
    };
    expect(ended("completed", "compensated", "compensated")).toBe(
      "Undone, item 2",
    );
    expect(ended("compensated", "completed", "in_doubt")).toBe(
      "In doubt · action needed, item 3",
    );
    expect(ended("reconciled", "compensated", "completed")).toBe(
      "Undone, item 2",
    );
    expect(ended("completed", "reconciled", "completed")).toBe(
      "Reconciled, item 2",
    );
    // One the run left open says nothing, the others do.
    expect(ended("completed", "completed", "open")).toBe("Completed, item 1");
  });

  it("says a step is running only while the run is", () => {
    const answer = structuredClone(answers.stopped);
    answer.transactions![0].participants[1].status = "dispatched";
    const run = simulatedRollback(answer, EXECUTIONS.stopped.workflow);
    expect(stepState(run, "add", true)?.text).toBe("Running");
    expect(stepState(run, "add", false)).toBeNull();
    // Nor does it wait for the step that can't be undone before it runs.
    answer.transactions![0].state = "deciding";
    expect(
      frameState(
        simulatedRollback(answer, EXECUTIONS.stopped.workflow),
        "move",
        true,
      )?.text,
    ).toBe("Waiting for the step that can't be undone");
  });

  it("says nothing of a step outside a transaction, nor of one it never reached", () => {
    expect(word("completed", "lookup")).toBeNull();
    expect(word("completed", "notify")).toBeNull();
    expect(stepState(rollback("definite"), "submit", false)).toBeNull();
    expect(frameState(rollback("definite"), "lookup", false)).toBeNull();
  });
});

describe.skipIf(!hasPython)("a step's tile once the run ended", () => {
  const end = (name: Name) =>
    finalRun(answers[name], EXECUTIONS[name].workflow);

  it("test_a_step_still_applied_in_a_transaction_in_doubt_shows_no_success_badge", () => {
    expect(tile(end("stuck"), "hold").badge).toEqual({
      kind: "in-doubt",
      tone: "warning",
      icons: ["lock"],
      text: "Still reserved",
    });
    expect(tile(end("unknown"), "add").badge).toEqual({
      kind: "in-doubt",
      tone: "warning",
      icons: ["warning"],
      text: "Not undone",
    });
    expect(tile(end("stuck"), "add")).toMatchObject({
      badge: { kind: "stuck", tone: "danger", text: "Undo failed" },
      borders: { error: true },
    });
  });

  it("shows an undone or released step as a warning, never as finished", () => {
    for (const [id, text] of [
      ["add", "Undone"],
      ["hold", "Released"],
    ])
      expect(tile(end("undone"), id)).toEqual({
        badge: { kind: "undone", tone: "warning", icons: ["undo"], text },
        borders: { selected: false, live: false, error: false },
      });
    expect(tile(end("completed"), "hold").badge).toMatchObject({
      kind: "done",
      text: "Confirmed",
    });
  });

  it("says of a failed step that its transaction was undone, with its own code", () => {
    for (const [name, id, code] of [
      ["undone", "submit", "REFUSED"],
      ["definite", "add", "VALIDATION"],
      // A step with nothing to undo, too.
      ["buildFails", "check", "NOT_ALLOWED"],
      // The run went on past the transaction.
      ["continued", "submit", "REFUSED"],
      ["loopStopped", "submit", "REFUSED"],
    ] as const) {
      const { workflow } = EXECUTIONS[name];
      const failure = runFailureOf(answers[name], id, workflow);
      expect(failure?.code, name).toBe(code);
      expect(tile(end(name), id, failure), name).toMatchObject({
        badge: {
          kind: "failed",
          tone: "danger",
          text: `Failed: ${code}. The transaction was undone.`,
        },
        borders: { error: true },
      });
    }
    // It may have changed something: undone, and it failed all the same.
    const may = runFailureOf(
      answers.mayHave,
      "add",
      EXECUTIONS.mayHave.workflow,
    );
    expect(tile(end("mayHave"), "add", may)).toMatchObject({
      badge: {
        kind: "failed",
        text: "Failed: BUSY. The transaction was undone.",
      },
      borders: { error: true },
    });
    // In doubt, nothing was undone: the step's own message says why it failed.
    const failure = runFailureOf(
      answers.stuck,
      "submit",
      EXECUTIONS.stuck.workflow,
    );
    expect(tile(end("stuck"), "submit", failure).badge?.text).toMatch(
      /^Failed: REFUSED, (?!.*transaction was undone)/,
    );
    expect(
      runFailureOf(answers.undone, "add", EXECUTIONS.undone.workflow),
    ).toBe(null);
  });

  it("keeps a block that continued as failed and continued, with its frame undone", () => {
    const run = end("continued");
    expect(run.status).toBe("succeeded");
    expect(run.handled).toMatchObject({
      move: { code: "WV-TXN-ROLLED-BACK", policy: "continue" },
    });
    expect(tileRun("move", run)).toBe("handled");
    expect(tileRun("summarize", run)).toBe("done");
    expect(frame("continued")).toBe("Undone/warning");
    // The failure it contained never stopped the run, which did get there.
    expect(run.current).toEqual([]);
    expect(tileRun("submit", run)).toBe("failed");
    expect(edgeRun({ leaves: "add", enters: "submit" }, run)).toBe("taken");
    expect(edgeRun({ leaves: "submit", enters: "notify" }, run)).toBe(
      "skipped",
    );
  });

  it("goes past every failure a transaction that continued was undone for", () => {
    expect(end("buildContinued")).toMatchObject({
      status: "succeeded",
      current: [],
      failed: ["check"],
    });
    // Another step of it that failed meanwhile, too.
    const both = structuredClone(answers.continued);
    Object.assign(both.trace[2], {
      status: "failed",
      error: both.trace[3].error,
    });
    both.transactions![0].participants[1].status = "failed";
    expect(finalRun(both, EXECUTIONS.continued.workflow).failed).toEqual([
      "add",
      "submit",
    ]);
    expect(
      groupsEntered({ ...end("buildContinued"), done: [] }, (id) =>
        id === "check" ? "route" : null,
      ),
    ).toEqual(new Set(["route"]));
  });

  it("stops where the run stopped, not where a transaction that continued failed", () => {
    const { workflow } = EXECUTIONS.failsLater;
    const run = end("failsLater");
    expect(run).toMatchObject({
      status: "failed",
      current: ["tell"],
      failed: ["submit"],
    });
    expect(runFailureOf(answers.failsLater, "tell", workflow)?.code).toBe(
      "WV-TASK-RETRIES-EXHAUSTED",
    );
    expect(runFailureOf(answers.failsLater, "submit", workflow)?.code).toBe(
      "REFUSED",
    );
    expect(transactionSummary(answers.failsLater)).toBeNull();
  });

  it("shows no transaction as running once the run stopped where it was asked to", () => {
    const run = end("stopped");
    expect(frameState(run.transactions!, "move", false)).toBeNull();
    expect(tile(run, "hold").badge?.kind).toBe("done");
    expect(stepState(run.transactions!, "hold", false)?.text).toBe("Done");
  });
});

describe("a step's run, as its transaction tells it", () => {
  it("keeps a failure its own On error handled, and a step that waits", () => {
    expect(transactionTileRun("handled", { tone: "danger" })).toBe("handled");
    expect(transactionTileRun("waiting", { tone: "live" })).toBe("waiting");
    expect(transactionTileRun(null, { tone: "live" })).toBe("live");
    expect(transactionTileRun("done", { tone: "danger" })).toBe("failed");
    expect(transactionTileRun("done", { tone: "warning" })).toBe("done");
    expect(transactionTileRun("done", null)).toBe("done");
  });

  it("shows no badge on a step that runs now, whatever an earlier item's was", () => {
    const left = {
      status: "undone",
      text: "Undone",
      tone: "warning",
      undone: true,
    } as const;
    const facts = {
      errors: [],
      warnings: [],
      setup: [],
      failure: null,
      stale: null,
      pinned: false,
      selected: false,
    };
    expect(stepBadge({ ...facts, run: "live" }, left)).toBeNull();
    expect(stepBadge({ ...facts, run: "done" }, left)?.text).toBe("Undone");
  });

  it("says in the frame's tooltip who acts on one in doubt", () => {
    const said = (state: "in_doubt" | "compensated", item: string) =>
      stateText({
        facts: { state },
        ...stateBadge({ state }),
        item,
      });
    expect(said("in_doubt", "")).toBe(
      "In doubt · action needed. In a real run, someone resolves this in Operate.",
    );
    expect(said("in_doubt", ", item 2")).toBe(
      "In doubt · action needed, item 2. In a real run, someone resolves this in Operate.",
    );
    expect(said("compensated", ", item 2")).toBe("Undone, item 2");
  });

  it("has an icon for how a transaction ended, and none while it runs", () => {
    expect(
      (
        [
          "open",
          "deciding",
          "committing",
          "compensating",
          "completed",
          "compensated",
          "cancelled",
          "in_doubt",
          "reconciled",
          "force_ended",
        ] as const
      ).map(stateIcon),
    ).toEqual([
      null,
      null,
      null,
      null,
      "ok",
      "undo",
      "undo",
      "critical",
      "ok",
      "off",
    ]);
  });
});

describe.skipIf(!hasPython)("the undo steps played back", () => {
  it("lights each step being undone in reverse, then its end word, as the frame counts them", () => {
    expect(
      frames("undone").map((run) => [
        ...moment(run, "hold", "add", "submit"),
        run.undoing ?? null,
      ]),
    ).toEqual([
      ["move", "", null, null, null, null],
      ["hold", "", "Running", null, null, null],
      ["add", "", "Done", "Running", null, null],
      [
        "submit",
        "Waiting for submit, which can't be undone",
        "Done",
        "Done",
        "Running",
        null,
      ],
      ["add", "Undoing · 1 of 2", "Done", "Undoing", "Failed", "add"],
      ["hold", "Undoing · 2 of 2", "Releasing", "Undone", "Failed", "hold"],
      ["submit", "Undone", "Released", "Undone", "Failed", null],
    ]);
  });

  it("draws the live border on the step being undone, and keeps the failed one's", () => {
    const run = frames("undone")[4];
    expect(tile(run, "add")).toEqual({
      badge: null,
      borders: { selected: false, live: true, error: false },
    });
    expect(tile(run, "submit").borders.error).toBe(true);
    expect(tile(run, "hold").badge?.kind).toBe("done");
  });

  it("draws the edge into that step back, the ones before it taken", () => {
    const run = frames("undone")[4];
    const into = (leaves: string, enters: string) =>
      edgeRun({ leaves, enters }, run, new Set(["move"]));
    expect(into("hold", "add")).toBe("undoing");
    expect(into("add", "submit")).toBe("taken");
    const next = frames("undone")[5];
    expect(edgeRun({ leaves: null, enters: "hold" }, next)).toBe("undoing");
    expect(edgeRun({ leaves: "hold", enters: "add" }, next)).toBe("taken");
    expect(
      edgeRun({ leaves: "hold", enters: "add" }, frames("undone").at(-1)!),
    ).toBe("taken");
  });

  it("confirms without drawing an edge back, as the transaction finishes", () => {
    const run = frames("completed").find(
      ({ current }, at, all) =>
        current[0] === "hold" && all[at - 1].current[0] === "notify",
    )!;
    expect(moment(run, "hold", "add")).toEqual([
      "hold",
      "Finishing",
      "Confirming",
      "Done",
    ]);
    expect(run.undoing).toBeUndefined();
    const next = frames("completed").at(-2)!;
    expect(moment(next, "hold")).toEqual([
      "summarize",
      "Completed",
      "Confirmed",
    ]);
  });

  it("starts with its first step, and ends with its last when nothing is confirmed", () => {
    const states = (name: Name) =>
      frames(name).map((run) => [run.current[0] ?? null, moment(run)[1]]);
    expect(states("completed").slice(0, 2)).toEqual([
      ["lookup", null],
      ["move", ""],
    ]);
    expect(states("nothingReserved")).toEqual([
      ["move", ""],
      ["add", ""],
      ["submit", "Waiting for submit, which can't be undone"],
      [null, "Completed"],
    ]);
  });

  it("reads a step that failed as it did until it is undone too", () => {
    expect(frames("mayHave").map((run) => moment(run, "hold", "add"))).toEqual([
      ["move", "", null, null],
      ["hold", "", "Running", null],
      ["add", "", "Done", "Running"],
      ["add", "Undoing · 1 of 2", "Done", "Undoing"],
      ["hold", "Undoing · 2 of 2", "Releasing", "Undone"],
      ["add", "Undone", "Released", "Undone"],
    ]);
    const run = playedBack(
      answers.mayHave,
      EXECUTIONS.mayHave.workflow,
      answers.mayHave.trace,
      [{ entry: 0 }, { entry: 1 }, { entry: 2 }, { phase: 1 }],
    )(3);
    expect(run.byStep.get("add")).toBe("unknown");
  });

  it("keeps a step the run left running as running, past the ones after it", () => {
    const answer = structuredClone(answers.undone);
    answer.trace[1].status = "running";
    const run = playedBack(
      answer,
      EXECUTIONS.undone.workflow,
      answer.trace,
      answer.trace.map((_, entry) => ({ entry })),
    )(3);
    expect(run.byStep.get("hold")).toBe("dispatched");
  });

  it("marks a step with nothing to undo as failed only once it failed", () => {
    const left = frames("buildFails").map(
      ({ transactions, ended }) =>
        stepState(transactions!, "check", !ended)?.tone ?? null,
    );
    expect(left).toEqual([null, null, null, "danger", "danger"]);
  });

  it("undoes each item of a loop inside it", () => {
    const { phases, byFrame } = rollback("loopInside");
    expect([...byFrame.keys()]).toEqual(["move"]);
    expect(phases.map(({ instanceKey }) => instanceKey)).toEqual([
      "add[2]#undo",
      "add[1]#undo",
      "add[0]#undo",
    ]);
    expect(
      frames("loopInside")
        .filter(({ undoing }) => undoing)
        .map((run) => moment(run)[1]),
    ).toEqual(["Undoing · 1 of 3", "Undoing · 2 of 3", "Undoing · 3 of 3"]);
    // It ends with the last item's step.
    expect(frames("loopOnly").map((run) => moment(run).join(" "))).toEqual([
      "move ",
      "each ",
      "add ",
      "add ",
      "add ",
      " Completed",
    ]);
    // Open while its steps run, whichever item they are of.
    expect(frames("loopInside")[2].transactions!.byFrame.get("move")).toEqual({
      state: "open",
      wentAhead: false,
    });
  });

  it("plays each item's own transaction in a loop, then the run goes on", () => {
    const run = frames("inLoop");
    const undo = run.findIndex(({ undoing }) => undoing === "add");
    expect(moment(run[undo], "hold", "submit")).toEqual([
      "add",
      "Undoing · 1 of 2",
      "Done",
      "Failed",
    ]);
    expect(frameState(run[undo].transactions!, "move", true)?.item).toBe("");
    // Item 3's own steps, not item 2's.
    const third = run.findIndex(
      ({ current, at }) => current[0] === "hold" && at === 10,
    );
    expect(moment(run[third], "hold", "add")).toEqual([
      "hold",
      "",
      "Running",
      null,
    ]);
    expect(frames("continued").map(({ current }) => current[0])).toEqual([
      "move",
      "hold",
      "add",
      "submit",
      "add",
      "hold",
      "summarize",
      undefined,
    ]);
    // The block is live until its undo steps played, then failed and continued.
    expect(
      frames("continued").map((each) =>
        transactionTileRun(
          tileRun("move", each),
          frameState(each.transactions!, "move", !each.ended),
        ),
      ),
    ).toEqual([...Array<string>(6).fill("live"), "handled", "handled"]);
  });

  it("shows how everything ended at once with reduced motion", () => {
    const all = frames("undone", true);
    expect(all).toHaveLength(1);
    expect(all[0].undoing).toBeUndefined();
    expect(moment(all[0], "hold", "add", "submit")).toEqual([
      "submit",
      "Undone",
      "Released",
      "Undone",
      "Failed",
    ]);
  });

  it("plays a run without transactions as before", () => {
    const plain = structuredClone(answers.completed);
    delete plain.transactions;
    delete plain.phases;
    const run = playbackFrames(plain, false);
    expect(run).toHaveLength(plain.trace.length + 1);
    expect(run.every(({ transactions }) => !transactions)).toBe(true);
  });
});

describe.skipIf(!hasPython)("how the run ended, in a sentence", () => {
  const said = (name: Name) => transactionSummary(answers[name]);
  it("says the transaction was undone, or is in doubt and why", () => {
    for (const name of ["undone", "definite", "loopStopped"] as const)
      expect(said(name), name).toEqual({
        text: "The simulated run failed: the transaction move was undone.",
        detail: null,
        inDoubt: false,
      });
    expect(said("stuck")).toEqual({
      text: "The transaction move is in doubt, so the simulated run stopped.",
      detail:
        "WV-TXN-UNDO-STUCK: A step's undo couldn't finish, so the transaction is in doubt.",
      inDoubt: true,
    });
    expect(said("unknown")?.detail).toBe(
      "WV-TXN-PIVOT-UNKNOWN: The step that can't be undone may or may not have happened.",
    );
    expect(said("confirmStuck")?.detail).toBe(
      "WV-TXN-CONFIRM-STUCK: A reservation couldn't be confirmed after the transaction went ahead.",
    );
    for (const name of ["completed", "continued", "stopped"] as const)
      expect(said(name), name).toBeNull();
    // The run didn't fail: nothing to say of it yet.
    expect(
      transactionSummary({ ...answers.undone, status: "blocked" }),
    ).toBeNull();
  });

  it("is what the live region says when the playback ends", () => {
    expect(finalAnnouncement(answers.undone)).toBe(
      "The simulated run failed: the transaction move was undone.",
    );
    expect(finalAnnouncement(answers.confirmStuck)).toBe(
      "The transaction move is in doubt, so the simulated run stopped. WV-TXN-CONFIRM-STUCK: A reservation couldn't be confirmed after the transaction went ahead.",
    );
    expect(finalAnnouncement(answers.completed)).toBe("Executed 7 steps.");
  });
});
vi.setConfig({ testTimeout: 20_000 });
