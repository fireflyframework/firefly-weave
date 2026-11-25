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
// Which decisions an incident in doubt offers, what each asks for before it
// can be sent, and the request it builds.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import type { IncidentView } from "../src/app/operate/incidents/incidents-model";
import {
  afterRevisionRefresh,
  checkForceEnd,
  checkReconcile,
  checkRetry,
  checkStepOutcome,
  checklistChanged,
  checklistRows,
  confirmReason,
  endingHelp,
  explainDecision,
  outputRefusalAt,
  retryLabel,
  transactionActions,
  type MissingPart,
  type ReconcileDraft,
} from "../src/app/operate/incidents/transaction-resolutions";
import { REASON_LIMIT } from "../src/app/operate/operate-limits";
import type { TransactionParticipantView } from "../src/app/operate/transactions/transaction-contracts";
import {
  called,
  caller,
  held,
  loop,
  others,
  runs,
  unknown,
} from "./transaction-incidents-fixture";

const corpus = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/transaction-corpus.json"),
    "utf8",
  ),
) as {
  codes: Record<string, string>;
  resolutions: Record<string, { kind: string; label: string; help: string }>;
};
const everything = () => true;
const only =
  (...held: string[]) =>
  (capability: string) =>
    held.includes(capability);
const coded = (code: string): IncidentView =>
  code === held.code ? held : { ...called, code };
const labels = (incident: IncidentView, can = everything) =>
  transactionActions(incident, can).map((offer) => offer.label);

describe("the decisions an incident in doubt offers", () => {
  it.each([
    ["WV-TXN-UNDO-STUCK", ["Retry the undo", "Mark as reconciled"]],
    ["WV-TXN-CANCEL-STUCK", ["Retry the release", "Mark as reconciled"]],
    ["WV-TXN-CONFIRM-STUCK", ["Retry the confirm", "Mark as reconciled"]],
    ["WV-TXN-AFTER-PIVOT-STUCK", ["Retry the step", "Mark as reconciled"]],
    ["WV-TXN-SETTLE-TIMEOUT", ["Wait again", "Mark as reconciled"]],
    ["WV-TXN-PIVOT-UNKNOWN", ["It happened", "It didn't happen"]],
    ["WV-TXN-RUN-OVERSIZED", []],
  ])("for %s, then Force end", (code, offered) => {
    expect(labels(coded(code))).toEqual([...offered, "Force end"]);
    const retry = transactionActions(coded(code), everything).find(
      (offer) => offer.action === "retry",
    );
    expect(retry?.label ?? null).toBe(retryLabel(code));
  });

  it("cover every code of a transaction in doubt, and only Force end for one Studio doesn't know", () => {
    for (const code of Object.keys(corpus.codes))
      expect(labels(coded(code)).at(-1)).toBe("Force end");
    expect(labels(coded("WV-TXN-SOMETHING-NEW"))).toEqual(["Force end"]);
  });

  it("none for an incident that isn't in doubt, or no longer is", () => {
    expect(labels(others[0])).toEqual([]);
    expect(labels({ ...called, status: "resolved" })).toEqual([]);
    expect(labels({ ...called, status: "closed" })).toEqual([]);
  });

  it("no Mark as reconciled without the record its checklist comes from", () => {
    const { transaction: _record, ...withheld } = called;
    expect(labels(withheld)).toEqual(["Retry the undo", "Force end"]);
  });

  it("send the platform's kinds, with the words the platform's corpus pins", () => {
    const offers = [
      ...transactionActions(called, everything),
      ...transactionActions(unknown, everything).slice(0, 2),
    ];
    for (const { action, kind, label, help } of offers)
      expect({ kind, label, help }, action).toEqual(corpus.resolutions[action]);
    const [wait] = transactionActions(caller, everything);
    expect({ kind: wait.kind, label: wait.label, help: wait.help }).toEqual(
      corpus.resolutions["wait_again"],
    );
    expect(wait.action).toBe("retry");
  });

  it("say what a retry runs again", () => {
    expect(
      ["UNDO", "CANCEL", "CONFIRM", "AFTER-PIVOT"].map(
        (part) =>
          transactionActions(coded(`WV-TXN-${part}-STUCK`), everything)[0].help,
      ),
    ).toEqual([
      "Runs the undo again with the same operation key. Only if the other system can take it again.",
      "Runs the release again with the same operation key. Only if the other system can take it again.",
      "Runs the confirm again with the same operation key. Only if the other system can take it again.",
      "Runs the step again with the same operation key. Only if the other system can take it again.",
    ]);
  });

  it("need the reconciler for all but a retry, and say what is missing", () => {
    const missing = (...held: string[]) =>
      Object.fromEntries(
        [
          ...transactionActions(called, only(...held)),
          ...transactionActions(unknown, only(...held)).slice(0, 2),
        ].map((offer) => [offer.action, offer.missing]),
      );
    const all = [
      "retry",
      "mark_reconciled",
      "force_end",
      "it_happened",
      "it_didnt_happen",
    ];
    const each = (words: string | null) =>
      Object.fromEntries(all.map((action) => [action, words]));
    expect(missing("incident.resolve", "transaction.reconcile")).toEqual(
      each(null),
    );
    expect(missing("incident.resolve")).toEqual({
      ...each("Needs the transaction reconciler role."),
      retry: null,
    });
    expect(missing("transaction.reconcile")).toEqual(
      each("Needs permission to resolve incidents."),
    );
    expect(missing()).toEqual(each("Needs permission to resolve incidents."));
  });
});

type Step = TransactionParticipantView;
const step = (
  key: string,
  mode: Step["mode"],
  status: Step["status"],
  attempts?: number,
): Step => ({
  node_id: key.replace(/\[.*/, ""),
  instance_key: key,
  mode,
  status,
  effect: "none",
  ...(attempts ? { attempts } : {}),
});
/** An incident in doubt about `key`, as the platform names it, with these steps. */
const about = (
  code: string,
  key: string,
  participants: Step[],
): IncidentView => ({
  ...called,
  code,
  node_id: key.replace(/\[.*/, "").replace(/#.*/, ""),
  instance_key: key,
  transaction: { ...called.transaction!, participants },
});
/** A record with one step of each way to be stuck, and steps that ended undone. */
const stuck = about("WV-TXN-UNDO-STUCK", "add#undo", [
  step("hold", "reserve", "cancel_stuck", 1),
  step("add", "compensate", "undo_stuck", 3),
  step("keep[2]", "reserve", "confirm_stuck", 4),
  step("note", "compensate", "undone"),
]);
const undoStuck = about("WV-TXN-UNDO-STUCK", "add#undo", [
  step("hold", "reserve", "done"),
  step("add", "compensate", "undo_stuck", 3),
  step("add2", "compensate", "undone"),
]);
const settleTimeout = about("WV-TXN-SETTLE-TIMEOUT", "move", [
  step("hold", "reserve", "done"),
  step("add", "compensate", "unknown"),
  step("add2", "compensate", "dispatched"),
]);
const confirmStuck = about("WV-TXN-CONFIRM-STUCK", "hold#confirm", [
  step("hold", "reserve", "confirm_stuck", 2),
  step("hold2", "reserve", "confirming"),
  step("submit", "irreversible", "done"),
]);
const cancelStuck = about("WV-TXN-CANCEL-STUCK", "hold[3]#cancel", [
  step("keep[3]", "reserve", "done"),
  step("hold[3]", "reserve", "cancel_stuck", 10),
  step("add[3]", "compensate", "undone"),
]);
const afterPivot = (notify: Step["status"]) =>
  about("WV-TXN-AFTER-PIVOT-STUCK", "notify", [
    step("hold", "reserve", "confirmed"),
    step("add", "compensate", "done"),
    step("submit", "irreversible", "done"),
    step("notify", "irreversible", notify),
  ]);

describe("the checklist", () => {
  const listed = (incident: IncidentView, as: "compensated" | "completed") =>
    Object.fromEntries(
      checklistRows(incident, as).map((row) => [
        row.instanceKey,
        row.outcomes.join(),
      ]),
    );
  it.each([
    ["an undo stuck", "Undone", undoStuck, { hold: "undone", add: "undone" }],
    [
      "an undo stuck",
      "Finished",
      undoStuck,
      { hold: "confirmed", add: "done", add2: "done" },
    ],
    [
      "a settle timeout",
      "Undone",
      settleTimeout,
      { hold: "undone", add: "undone", add2: "undone" },
    ],
    [
      "a settle timeout",
      "Finished",
      settleTimeout,
      { hold: "confirmed", add: "done", add2: "done" },
    ],
    [
      "a confirm stuck",
      "Undone",
      confirmStuck,
      { hold: "undone", hold2: "undone", submit: "undone" },
    ],
    [
      "a confirm stuck",
      "Finished",
      confirmStuck,
      { hold: "confirmed", hold2: "confirmed" },
    ],
    [
      "a step of unknown outcome after the point of no return",
      "Undone",
      afterPivot("unknown"),
      { hold: "undone", add: "undone", submit: "undone", notify: "undone" },
    ],
    [
      "a step of unknown outcome after the point of no return",
      "Finished",
      afterPivot("unknown"),
      { notify: "done" },
    ],
    [
      "a step that failed after the point of no return",
      "Undone",
      afterPivot("failed"),
      { hold: "undone", add: "undone", submit: "undone" },
    ],
    [
      "a step that failed after the point of no return",
      "Finished",
      afterPivot("failed"),
      { notify: "done" },
    ],
    [
      "a release stuck",
      "Undone",
      cancelStuck,
      { "keep[3]": "undone", "hold[3]": "undone" },
    ],
    [
      "a release stuck",
      "Finished",
      cancelStuck,
      { "keep[3]": "confirmed", "hold[3]": "confirmed", "add[3]": "done" },
    ],
  ] as const)(
    "of %s lists for %s what Weave doesn't know ended that way",
    (_what, ending, incident, steps) => {
      expect(
        listed(incident, ending === "Undone" ? "compensated" : "completed"),
      ).toEqual(steps);
    },
  );

  const STATUSES = [
    "pending",
    "dispatched",
    "done",
    "failed",
    "unknown",
    "skipped",
    "cancelling",
    "cancelled",
    "cancel_stuck",
    "undoing",
    "undone",
    "undo_stuck",
    "confirming",
    "confirmed",
    "confirm_stuck",
  ] as const;
  it.each([
    ["compensated", "reserve", "undone", "cancelled"],
    ["compensated", "compensate", "undone", "cancelled"],
    ["compensated", "irreversible", "undone", "cancelled"],
    ["completed", "reserve", "confirmed"],
    ["completed", "compensate", "confirmed", "done"],
    ["completed", "irreversible", "confirmed", "done"],
  ] as const)(
    "as %s leaves out a %s step only when it ended %s, or nothing of it stays",
    (as, mode, ...ended) => {
      const left: readonly string[] = [
        ...ended,
        "failed",
        "skipped",
        "pending",
      ];
      const incident = about(
        "WV-TXN-UNDO-STUCK",
        "other#undo",
        STATUSES.map((status) => step(status, mode, status)),
      );
      expect(Object.keys(listed(incident, as))).toEqual(
        STATUSES.filter((status) => !left.includes(status)),
      );
      expect(new Set(Object.values(listed(incident, as)))).toEqual(
        new Set([
          as === "compensated"
            ? "undone"
            : mode === "reserve"
              ? "confirmed"
              : "done",
        ]),
      );
    },
  );

  it("asks of a failed step only when the incident is about that one, in its own iteration", () => {
    const failed = about("WV-TXN-AFTER-PIVOT-STUCK", "notify[2]", [
      step("notify[1]", "irreversible", "failed"),
      step("notify[2]", "irreversible", "failed"),
      step("keep[2]", "reserve", "failed"),
    ]);
    expect(listed(failed, "completed")).toEqual({ "notify[2]": "done" });
    // The platform's own row names the whole instance as its step.
    const { instance_key: _key, ...row } = failed;
    expect(listed({ ...row, node_id: "keep[2]#confirm" }, "completed")).toEqual(
      { "keep[2]": "confirmed" },
    );
  });

  it("asks of a step whose status Studio doesn't know, for either ending", () => {
    const newer = about("WV-TXN-UNDO-STUCK", "other#undo", [
      step("add", "compensate", "held_open" as Step["status"]),
    ]);
    expect(listed(newer, "compensated")).toEqual({ add: "undone" });
    expect(listed(newer, "completed")).toEqual({ add: "done" });
  });

  it("is empty without the record it is built from", () => {
    expect(checklistRows(held, "compensated")).toEqual([]);
  });

  it("says what each line confirms, and how Weave left its step", () => {
    const said = (incident: IncidentView, as: "compensated" | "completed") =>
      checklistRows(incident, as).map((row) => [row.sentence, row.caption]);
    const key = (rest: string) => `operation key ${runs.called}:${rest}`;
    expect(said(stuck, "compensated")).toEqual([
      [
        "I confirmed hold was reversed in the other system.",
        `Release failed · ${key("hold#cancel")}`,
      ],
      [
        "I confirmed add was reversed in the other system.",
        `Undo failed after 3 tries · ${key("add#undo")}`,
      ],
      [
        "I confirmed keep[2] was reversed in the other system.",
        `Confirm failed after 4 tries · ${key("keep[2]#confirm")}`,
      ],
    ]);
    expect(said(stuck, "completed")).toEqual([
      [
        "I confirmed hold was confirmed in the other system.",
        `Release failed · ${key("hold#cancel")}`,
      ],
      [
        "I confirmed add finished in the other system.",
        `Undo failed after 3 tries · ${key("add#undo")}`,
      ],
      [
        "I confirmed keep[2] was confirmed in the other system.",
        `Confirm failed after 4 tries · ${key("keep[2]#confirm")}`,
      ],
      [
        "I confirmed note finished in the other system.",
        `Undone · ${key("note#undo")}`,
      ],
    ]);
    // A reservation nobody released, and a step of unknown outcome.
    expect(said(settleTimeout, "compensated").slice(0, 2)).toEqual([
      [
        "I confirmed hold was reversed in the other system.",
        `Still reserved · ${key("hold")}`,
      ],
      [
        "I confirmed add was reversed in the other system.",
        `Unknown · ${key("add")}`,
      ],
    ]);
    // What went ahead is done, whatever the transaction's state.
    expect(said(afterPivot("failed"), "compensated").slice(0, 2)).toEqual([
      [
        "I confirmed hold was reversed in the other system.",
        `Confirmed · ${key("hold#confirm")}`,
      ],
      [
        "I confirmed add was reversed in the other system.",
        `Done · ${key("add")}`,
      ],
    ]);
  });

  it("asks of a step still running, when undone, that it is stopped too", () => {
    const running = about("WV-TXN-SETTLE-TIMEOUT", "move", [
      step("add", "compensate", "dispatched", 2),
      step("hold", "reserve", "cancelling"),
      step("add2", "compensate", "undoing", 3),
      step("hold2", "reserve", "confirming"),
      step("add3", "compensate", "unknown", 2),
    ]);
    const key = (rest: string) => `operation key ${runs.called}:${rest}`;
    expect(
      checklistRows(running, "compensated").map((row) => [
        row.sentence,
        row.caption,
      ]),
    ).toEqual([
      [
        "I confirmed add is stopped and its change is reversed in the other system.",
        // A try that runs hasn't ended anything.
        `Running · 2 tries · ${key("add")}`,
      ],
      [
        "I confirmed hold is stopped and its change is reversed in the other system.",
        `Releasing · ${key("hold#cancel")}`,
      ],
      [
        "I confirmed add2 is stopped and its change is reversed in the other system.",
        `Undoing · 3 tries · ${key("add2#undo")}`,
      ],
      [
        "I confirmed hold2 is stopped and its change is reversed in the other system.",
        `Confirming · ${key("hold2#confirm")}`,
      ],
      [
        "I confirmed add3 was reversed in the other system.",
        `Unknown · 2 tries · ${key("add3")}`,
      ],
    ]);
    expect(checklistRows(running, "completed")[0].sentence).toBe(
      "I confirmed add finished in the other system.",
    );
  });
});

describe("how it ended", () => {
  const undone = (incident: IncidentView) =>
    endingHelp("compensated", incident.transaction);
  const finished = (incident: IncidentView) =>
    endingHelp("completed", incident.transaction);

  it("undone reversed or released every step, and by hand those that can't be undone", () => {
    expect(undone(undoStuck)).toBe("Every step was reversed or released.");
    expect(undone(confirmStuck)).toBe(
      "Every step was reversed or released. The step that can't be undone was reversed by hand.",
    );
    expect(undone(afterPivot("done"))).toBe(
      "Every step was reversed or released. The steps that can't be undone were reversed by hand.",
    );
    // One that failed, or may not have happened, isn't done.
    for (const status of ["failed", "unknown"] as const)
      expect(undone(afterPivot(status))).toBe(undone(confirmStuck));
    expect(undone(unknown)).toBe("Every step was reversed or released.");
  });

  it("finished went through, whatever its steps", () => {
    for (const incident of [undoStuck, confirmStuck, held])
      expect(finished(incident)).toBe("Every step went through.");
  });
});

const rows = () => checklistRows(stuck, "compensated");
const draft = (change: Partial<ReconcileDraft> = {}): ReconcileDraft => ({
  as: "compensated",
  ticked: new Set(rows().map((row) => row.instanceKey)),
  blockOutput: "",
  reason: "Checked the target system",
  evidence: " OPS-2304 ",
  ...change,
});

describe("Mark as reconciled", () => {
  it("sends how it ended, the ticked steps and nothing else", () => {
    expect(checkReconcile(draft(), rows())).toEqual({
      request: {
        kind: "accept_reconciled_result",
        reason: "Checked the target system",
        evidence_reference: "OPS-2304",
        output: {
          as: "compensated",
          participants: [
            { instance_key: "hold", outcome: "undone" },
            { instance_key: "add", outcome: "undone" },
            { instance_key: "keep[2]", outcome: "undone" },
          ],
        },
      },
    });
    const finished = checklistRows(stuck, "completed");
    expect(
      checkReconcile(
        draft({
          as: "completed",
          blockOutput: '{"quantity": 40}',
          ticked: new Set(finished.map((row) => row.instanceKey)),
        }),
        finished,
      ),
    ).toEqual({
      request: {
        kind: "accept_reconciled_result",
        reason: "Checked the target system",
        evidence_reference: "OPS-2304",
        output: {
          as: "completed",
          participants: [
            { instance_key: "hold", outcome: "confirmed" },
            { instance_key: "add", outcome: "done" },
            { instance_key: "keep[2]", outcome: "confirmed" },
            { instance_key: "note", outcome: "done" },
          ],
          blockOutput: { quantity: 40 },
        },
      },
    });
  });

  it("takes null as the result of a finished transaction, and a record with no step to check", () => {
    expect(
      checkReconcile(
        draft({ as: "completed", blockOutput: " null ", ticked: new Set() }),
        [],
      ),
    ).toMatchObject({
      request: {
        output: { as: "completed", participants: [], blockOutput: null },
      },
    });
  });

  it("lists what it lacks in the form's order", () => {
    const nothing = draft({
      as: "",
      ticked: new Set(["add"]),
      reason: " ",
      evidence: "",
    });
    expect(checkReconcile(nothing, rows())).toEqual({
      missing: [
        { kind: "as" },
        { kind: "tick", steps: ["hold", "keep[2]"] },
        { kind: "field", label: "Reason" },
        { kind: "field", label: "Evidence reference" },
      ],
      problems: {},
    });
    expect(
      checkReconcile({ ...nothing, as: "completed", blockOutput: " " }, rows()),
    ).toEqual({
      missing: [
        { kind: "tick", steps: ["hold", "keep[2]"] },
        { kind: "field", label: "Result of the transaction" },
        { kind: "field", label: "Reason" },
        { kind: "field", label: "Evidence reference" },
      ],
      problems: {},
    });
  });

  it("refuses a result that isn't JSON, and one for a transaction that was undone", () => {
    expect(
      checkReconcile(
        draft({ as: "completed", blockOutput: "{quantity: 40}" }),
        rows(),
      ),
    ).toEqual({
      missing: [],
      problems: {
        blockOutput: 'Enter the result as JSON, for example {"quantity": 40}.',
      },
    });
    expect(
      checkReconcile(draft({ blockOutput: '{"quantity": 40}' }), rows()),
    ).toEqual({
      missing: [],
      problems: {
        blockOutput: "A transaction that was undone has no result.",
      },
    });
  });

  it("refuses a result that would be sent as another one", () => {
    const problem = (blockOutput: string) =>
      checkReconcile(draft({ as: "completed", blockOutput }), rows());
    expect(problem('{"order": 12345678901234567890}')).toMatchObject({
      missing: [],
      problems: {
        blockOutput: expect.stringMatching(/can't be sent exactly as typed/),
      },
    });
    expect(problem('{"order": 1, "order": 2}')).toMatchObject({
      problems: { blockOutput: expect.stringMatching(/the same key twice/) },
    });
  });

  it("refuses a reason or an evidence reference the platform doesn't take", () => {
    const long = "x".repeat(REASON_LIMIT + 1);
    expect(
      checkReconcile(draft({ reason: long, evidence: long }), rows()),
    ).toEqual({
      missing: [],
      problems: {
        reason: "Keep the reason to 2,000 characters or fewer.",
        evidence: "Keep the evidence reference to 2,000 characters or fewer.",
      },
    });
  });

  it("keeps all but the ticks once the incident is read again", () => {
    const typed = draft({ as: "completed", blockOutput: "{}" });
    expect(afterRevisionRefresh(typed)).toEqual({
      ...typed,
      ticked: new Set(),
    });
    // What was typed is not touched in place: a dialog redraws from a new draft.
    expect(typed.ticked.size).toBe(3);
  });

  describe("read again at the same revision", () => {
    const moved = (change: (item: Step) => Partial<Step>): IncidentView => ({
      ...undoStuck,
      transaction: {
        ...undoStuck.transaction!,
        participants: undoStuck.transaction!.participants.map((item) => ({
          ...item,
          ...change(item),
        })),
      },
    });
    const of = (key: string, change: Partial<Step>) =>
      moved((item) => (item.instance_key === key ? change : {}));

    it.each([
      ["a step moved on", of("hold", { status: "confirming" })],
      ["a step was tried again", of("add", { attempts: 4 })],
      // Listed when finished only.
      ["a step was skipped", of("add2", { status: "skipped" })],
      [
        "a step joined",
        {
          ...undoStuck,
          transaction: {
            ...undoStuck.transaction!,
            participants: [
              ...undoStuck.transaction!.participants,
              step("add3", "compensate", "dispatched"),
            ],
          },
        },
      ],
    ] as const)("has another checklist once %s", (_why, read) => {
      expect(checklistChanged(undoStuck, read)).toBe(true);
    });

    it("has the same checklist while what it lists is the same", () => {
      expect(
        checklistChanged(
          undoStuck,
          moved(() => ({})),
        ),
      ).toBe(false);
      // Nothing it lists says these.
      expect(
        checklistChanged(undoStuck, {
          ...of("add2", { effect: "unknown" }),
          run_status: "waiting",
          updated_at: "2026-11-22T10:53:30Z",
        }),
      ).toBe(false);
    });
  });

  it("has another checklist at another revision, whatever it lists", () => {
    expect(checklistChanged(undoStuck, { ...undoStuck, revision: 2 })).toBe(
      true,
    );
  });
});

describe("the step that can't be undone", () => {
  const said = {
    happened: true,
    output: '{"order": "O-1"}',
    reason: "It is there",
    evidence: "Search at 10:52",
  };

  it("happened: its output, the reason and the evidence", () => {
    expect(checkStepOutcome(said)).toEqual({
      request: {
        kind: "accept_reconciled_result",
        reason: "It is there",
        evidence_reference: "Search at 10:52",
        output: { order: "O-1" },
      },
    });
  });

  it("didn't happen: the reason and the evidence, and never an output", () => {
    expect(checkStepOutcome({ ...said, happened: false })).toEqual({
      request: {
        kind: "accept_reconciled_failure",
        reason: "It is there",
        evidence_reference: "Search at 10:52",
      },
    });
  });

  it("lists what each lacks in the form's order", () => {
    const empty = { output: "", reason: "", evidence: " " };
    expect(checkStepOutcome({ ...empty, happened: true })).toEqual({
      missing: [
        { kind: "field", label: "Result of the step" },
        { kind: "field", label: "Reason" },
        { kind: "field", label: "Evidence reference" },
      ],
      problems: {},
    });
    expect(checkStepOutcome({ ...empty, happened: false })).toEqual({
      missing: [
        { kind: "field", label: "Reason" },
        { kind: "field", label: "Evidence reference" },
      ],
      problems: {},
    });
  });

  it("happened with a result that isn't JSON says so", () => {
    expect(checkStepOutcome({ ...said, output: "done" })).toEqual({
      missing: [],
      problems: {
        output: 'Enter the result as JSON, for example {"quantity": 40}.',
      },
    });
  });

  it("happened with a result that would be sent as another one says so", () => {
    expect(checkStepOutcome({ ...said, output: "1e999" })).toMatchObject({
      missing: [],
      problems: {
        output: expect.stringMatching(/can't be sent exactly as typed/),
      },
    });
  });
});

describe("Retry and Force end", () => {
  it("a retry sends its reason alone", () => {
    expect(checkRetry({ reason: "The system is back" })).toEqual({
      request: { kind: "retry_safe", reason: "The system is back" },
    });
    expect(checkRetry({ reason: " " })).toEqual({
      missing: [{ kind: "field", label: "Reason" }],
      problems: {},
    });
  });

  it("Force end needs the run's short ID typed, whatever its case and spaces", () => {
    const end = (typed: string, reason = "Closed by hand") =>
      checkForceEnd({ reason, typed }, runs.unknown);
    expect(end(" 7B3E9D10 ")).toEqual({
      request: { kind: "terminate", reason: "Closed by hand" },
    });
    for (const typed of ["", "7b3e9d1", "9a01c7d2", runs.unknown])
      expect(end(typed)).toEqual({
        missing: [{ kind: "typed" }],
        problems: {},
      });
    expect(end("", "")).toEqual({
      missing: [{ kind: "field", label: "Reason" }, { kind: "typed" }],
      problems: {},
    });
  });
});

describe("why a confirm waits", () => {
  const tick = (...steps: string[]): MissingPart => ({ kind: "tick", steps });
  const field = (
    label: Extract<MissingPart, { kind: "field" }>["label"],
  ): MissingPart => ({ kind: "field", label });

  it("names only what is missing", () => {
    const reason = (...missing: MissingPart[]) =>
      confirmReason("mark_reconciled", missing);
    expect(reason()).toBeNull();
    expect(reason(tick("add"), field("Evidence reference"))).toBe(
      "To mark it as reconciled, tick the line for `add` and fill in Evidence reference.",
    );
    expect(
      reason(
        { kind: "as" },
        tick("hold", "keep[2]"),
        field("Result of the transaction"),
        field("Reason"),
      ),
    ).toBe(
      "To mark it as reconciled, choose how it ended, tick 2 more lines, fill in Result of the transaction and fill in Reason.",
    );
    expect(reason({ kind: "refresh" })).toBe(
      "To mark it as reconciled, refresh the incident.",
    );
  });

  it("leads with each decision", () => {
    const refresh: MissingPart[] = [{ kind: "refresh" }];
    expect(
      (
        ["wait_again", "it_happened", "it_didnt_happen", "force_end"] as const
      ).map((action) => confirmReason(action, refresh)),
    ).toEqual([
      "To wait again, refresh the incident.",
      "To record that it happened, refresh the incident.",
      "To record that it didn't happen, refresh the incident.",
      "To force end it, refresh the incident.",
    ]);
    for (const code of Object.keys(corpus.codes)) {
      const label = retryLabel(code);
      if (label && label !== "Wait again")
        expect(confirmReason("retry", [field("Reason")], label)).toBe(
          `To ${label.toLowerCase()}, fill in Reason.`,
        );
    }
    expect(
      confirmReason("force_end", [field("Reason"), { kind: "typed" }]),
    ).toBe("To force end it, fill in Reason and type the run ID.");
  });
});

describe("a refused decision", () => {
  const refused = (status: number, code: string, message: string) => ({
    status,
    code,
    message,
  });

  it("says a step that is running again in plain words, and keeps the platform's words for any other state", () => {
    const state = (message: string) =>
      explainDecision(
        refused(409, "WV-RUNTIME-STATE", message),
        transactionActions(called, everything)[0],
      ).message;
    for (const running of [
      "The latest attempt is still running",
      "The call's child run has not ended",
    ])
      expect(state(running)).toBe(
        "The step is running again. Wait for it to finish, then decide.",
      );
    expect(state("Incident is no longer active")).toBe(
      "Incident is no longer active",
    );
  });

  it("names the access its decision needs", () => {
    const forbidden = refused(403, "WV-FORBIDDEN", "Not allowed");
    const [retry, mark] = transactionActions(called, everything);
    expect(explainDecision(forbidden, retry)).toEqual({
      ...forbidden,
      message:
        "You need incident.resolve in this environment. Ask an administrator in Settings › People and access.",
    });
    expect(explainDecision(forbidden, mark).message).toBe(
      "You need incident.resolve and transaction.reconcile in this environment. Ask an administrator in Settings › People and access.",
    );
  });

  it("leaves every other refusal in the platform's words", () => {
    const [retry] = transactionActions(called, everything);
    const other = refused(
      409,
      "WV-RUNTIME-RECONCILIATION_REQUIRED",
      "Evidence is required",
    );
    expect(explainDecision(other, retry)).toBe(other);
  });

  it("about its output belongs to the result, or to the checklist when it names a step or there is no result", () => {
    const at = (message: string, as: ReconcileDraft["as"]) =>
      outputRefusalAt(message, as, rows());
    const record = "The reconciliation record doesn't fit the transaction";
    expect(at(record, "completed")).toBe("blockOutput");
    expect(at(record, "compensated")).toBe("checklist");
    expect(at("Nothing says how add ended", "completed")).toBe("checklist");
    expect(at("The record leaves out add.", "completed")).toBe("checklist");
    expect(at("keep[2] is not a reservation", "completed")).toBe("checklist");
    // A word that holds a step's name doesn't name it.
    expect(at("/address must be a text, holding one", "completed")).toBe(
      "blockOutput",
    );
  });

  it("names a step by its key, with its phase, in quotes or as an owner, and no other one", () => {
    const at = (message: string) =>
      outputRefusalAt(message, "completed", rows());
    for (const name of ["add#undo", "'add'", "`keep[2]#confirm`", "add's"])
      expect(at(`Nothing says how ${name} ended`), name).toBe("checklist");
    for (const name of ["add2", "keep[3]", "keep", "hold.value"])
      expect(at(`Nothing says how ${name} ended`), name).toBe("blockOutput");
  });

  it("isn't about the steps whose names are words of the platform's own sentences", () => {
    const words = checklistRows(
      about(
        "WV-TXN-UNDO-STUCK",
        "record#undo",
        [
          "record",
          "fit",
          "transaction",
          "t",
          "output",
          "limits",
          "exceeds",
        ].map((key) => step(key, "compensate", "undo_stuck")),
      ),
      "completed",
    );
    for (const sentence of [
      "The reconciliation record doesn't fit the transaction",
      "Resolution output exceeds runtime limits",
    ])
      expect(outputRefusalAt(sentence, "completed", words), sentence).toBe(
        "blockOutput",
      );
    // Another sentence may name one of them.
    expect(
      outputRefusalAt("Nothing says how record ended", "completed", words),
    ).toBe("checklist");
    expect(outputRefusalAt("Nothing doesn't end", "completed", words)).toBe(
      "blockOutput",
    );
  });
});
