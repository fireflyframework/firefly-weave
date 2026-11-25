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
import type {
  IncidentTransaction,
  IncidentView,
} from "../src/app/operate/incidents/incidents-model";
import {
  afterRevisionRefresh,
  checkForceEnd,
  checkReconcile,
  checkRetry,
  checkStepOutcome,
  checklistRows,
  confirmReason,
  explainDecision,
  outputRefusalAt,
  retryLabel,
  transactionActions,
  type MissingPart,
  type ReconcileDraft,
} from "../src/app/operate/incidents/transaction-resolutions";
import { REASON_LIMIT } from "../src/app/operate/operate-limits";
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

const record = called.transaction!;
/** A record with one step of each way to be stuck, and steps that aren't. */
const stuck: IncidentTransaction = {
  ...record,
  participants: [
    { ...record.participants[0], status: "cancel_stuck", attempts: 1 },
    record.participants[1],
    {
      ...record.participants[0],
      node_id: "keep",
      instance_key: "keep[2]",
      status: "confirm_stuck",
      attempts: 4,
    },
    { ...record.participants[1], node_id: "note", instance_key: "note" },
  ].map((participant, index) =>
    index === 3 ? { ...participant, status: "undone" } : participant,
  ),
};

describe("the checklist", () => {
  it("lists each step Weave couldn't finish, as it ended when undone", () => {
    expect(checklistRows(stuck, "compensated", runs.called)).toEqual([
      {
        instanceKey: "hold",
        stepId: "hold",
        sentence: "I confirmed hold was reversed in the other system.",
        caption: `Release failed · operation key ${runs.called}:hold#cancel`,
        operationKey: `${runs.called}:hold#cancel`,
        outcomes: ["undone"],
      },
      {
        instanceKey: "add",
        stepId: "add",
        sentence: "I confirmed add was reversed in the other system.",
        caption: `Undo failed after 3 tries · operation key ${runs.called}:add#undo`,
        operationKey: `${runs.called}:add#undo`,
        outcomes: ["undone"],
      },
      {
        instanceKey: "keep[2]",
        stepId: "keep",
        sentence: "I confirmed keep[2] was reversed in the other system.",
        caption: `Confirm failed after 4 tries · operation key ${runs.called}:keep[2]#confirm`,
        operationKey: `${runs.called}:keep[2]#confirm`,
        outcomes: ["undone"],
      },
    ]);
  });

  it("asks what a finished one did: a reservation confirmed, any other step finished", () => {
    expect(
      checklistRows(stuck, "completed", runs.called).map((row) => [
        row.sentence,
        row.outcomes,
      ]),
    ).toEqual([
      ["I confirmed hold was confirmed in the other system.", ["confirmed"]],
      ["I confirmed add finished in the other system.", ["done"]],
      ["I confirmed keep[2] was confirmed in the other system.", ["confirmed"]],
    ]);
  });

  it("is empty when no step is stuck", () => {
    expect(
      checklistRows(caller.transaction!, "compensated", runs.caller),
    ).toEqual([]);
  });
});

const rows = () => checklistRows(stuck, "compensated", runs.called);
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
    const finished = checklistRows(stuck, "completed", runs.called);
    expect(
      checkReconcile(
        draft({ as: "completed", blockOutput: '{"quantity": 40}' }),
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
          ],
          blockOutput: { quantity: 40 },
        },
      },
    });
  });

  it("takes null as the result of a finished transaction, and a record with no stuck step", () => {
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
});
