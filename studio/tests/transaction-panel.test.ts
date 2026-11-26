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
// What the Incidents drawer says about a transaction in doubt: its sentence,
// the related incidents and what to resolve first, the record of its steps,
// and the decision a person or a workflow recorded.
import "@angular/compiler";
import { describe, expect, it } from "vitest";
import { panelView } from "../src/app/operate/incidents/transaction-panel";
import {
  decisionText,
  retryLabel,
} from "../src/app/operate/incidents/transaction-resolutions";
import type { IncidentView } from "../src/app/operate/incidents/incidents-model";
import corpus from "./fixtures/transaction-corpus.json";
import {
  called,
  caller,
  held,
  loop,
  others,
  runs,
  unknown,
} from "./transaction-incidents-fixture";

const resolved = (
  incident: IncidentView,
  resolution: Partial<NonNullable<IncidentView["resolution"]>>,
): IncidentView => ({
  ...incident,
  status: "resolved",
  resolution: {
    receipt_id: "93333333-3333-4333-8333-333333333333",
    kind: "retry_safe",
    reason: "The target system is back",
    ...resolution,
  },
});

describe("what the drawer says of a transaction in doubt", () => {
  it("reads the platform's sentence for its code, while a person is waited for", () => {
    for (const incident of [unknown, caller, called, loop, held])
      expect(panelView(incident)?.sentence, incident.code).toBe(
        (corpus.codes as Record<string, string>)[incident.code],
      );
    // Decided, it no longer is in doubt.
    expect(
      panelView(resolved(called, { kind: "retry_safe" }))?.sentence,
    ).toBeNull();
  });

  it("says nothing of an incident with no transaction", () => {
    expect(panelView(others[0])).toBeNull();
  });

  it("a settle timeout waits on the called run's incident, named by its step without its undo", () => {
    expect(panelView(caller)?.links).toEqual([
      {
        title: "Waits on",
        hint: "Resolve these first: this run waits for them.",
        more: null,
        items: [
          {
            link: caller.waits_on![0],
            place: "add in run 9a01c7d2",
            status: "Active",
            tone: "danger",
          },
        ],
      },
    ]);
    const suffixed: IncidentView = {
      ...caller,
      waits_on: [{ ...caller.waits_on![0], node_id: "add#undo" }],
    };
    expect(panelView(suffixed)?.links[0].items[0].place).toBe(
      "add in run 9a01c7d2",
    );
  });

  it("a called run's incident is held by its caller's settle timeout", () => {
    expect(panelView(called)?.links).toEqual([
      {
        title: "Held by",
        hint: "That run waits for this one: resolve this incident first.",
        more: null,
        items: [
          {
            link: called.held_by,
            place: "move in run 6d18b3f0",
            status: "Active",
            tone: "danger",
          },
        ],
      },
    ]);
  });

  it("a run in the middle shows both, what it waits on first", () => {
    const middle: IncidentView = { ...caller, held_by: called.held_by };
    expect(panelView(middle)?.links.map((section) => section.title)).toEqual([
      "Waits on",
      "Held by",
    ]);
  });

  it("links withheld say that called runs wait, and links past the 20 it names that more do", () => {
    const { waits_on: _links, ...withheld } = caller;
    const without = { ...withheld, waits_on_more: true };
    for (const none of [without, { ...without, waits_on: [] }])
      expect(panelView(none)?.links).toEqual([
        {
          title: "Waits on",
          hint: null,
          more: "Called runs wait in doubt. Open the run to see them.",
          items: [],
        },
      ]);
    const full = panelView({ ...caller, waits_on_more: true })!.links[0];
    expect(full.items).toHaveLength(1);
    expect(full.more).toBe(
      "More called runs wait in doubt. Open the run to see them.",
    );
    expect(full.hint).toBe("Resolve these first: this run waits for them.");
    // Nothing to link and nothing more: no section at all.
    expect(panelView(unknown)?.links).toEqual([]);
  });

  it("the record names each step's key with the incident's whole run ID, and what stays applied", () => {
    const record = panelView(called)!.record!;
    expect(record.title).toBe("apply");
    expect(record.badge).toEqual({
      text: "In doubt · action needed",
      tone: "danger",
    });
    expect(record.rows.map((row) => row.operationKey)).toEqual([
      `${runs.called}:hold`,
      `${runs.called}:add#undo`,
    ]);
    const [hold, add] = record.rows;
    expect([hold.undo?.text, hold.forward.icon]).toEqual([
      "Still reserved",
      null,
    ]);
    expect([add.undo?.text, add.attempts, add.forward.icon]).toEqual([
      "Undo failed",
      "3 tries",
      null,
    ]);
    const waiting = panelView(caller)!.record!.rows;
    expect(waiting.map((row) => [row.pending, row.childRunId])).toEqual([
      [false, null],
      [true, runs.called],
      [false, null],
    ]);
  });

  it("a run held whole has no record: its transactions are held as they are", () => {
    const heldText =
      "Its transactions are held as they are. Open the run to see each one.";
    expect(panelView(held)).toMatchObject({ record: null, held: heldText });
    // Nor does a record without steps hold one.
    expect(
      panelView({
        ...held,
        transaction: {
          instance_key: "move",
          state: "in_doubt",
          participants: [],
          pending: [],
        },
      }),
    ).toMatchObject({ record: null, held: heldText });
    expect(panelView(unknown)?.held).toBeNull();
    // Force ended, nothing is held any more.
    expect(panelView(resolved(held, { kind: "terminate" }))?.held).toBeNull();
    // A record the platform left out is neither: the run may still show it.
    const { transaction: _record, ...bare } = unknown;
    expect(panelView(bare)).toMatchObject({ record: null, held: null });
  });

  it("shows the business key as text, or None", () => {
    expect(panelView(called)?.businessKey).toBe("T-1029");
    expect(panelView(held)?.businessKey).toBe("None");
  });
});

describe("a recorded decision", () => {
  it("names a retry by the incident's code, and Wait again after a settle timeout", () => {
    expect(
      Object.fromEntries(
        Object.keys(corpus.codes).map((code) => [code, retryLabel(code)]),
      ),
    ).toEqual({
      ...Object.fromEntries(
        Object.keys(corpus.codes).map((code) => [code, null]),
      ),
      "WV-TXN-UNDO-STUCK": "Retry the undo",
      "WV-TXN-CANCEL-STUCK": "Retry the release",
      "WV-TXN-CONFIRM-STUCK": "Retry the confirm",
      "WV-TXN-AFTER-PIVOT-STUCK": "Retry the step",
      "WV-TXN-SETTLE-TIMEOUT": "Wait again",
    });
    expect(decisionText(resolved(caller, { kind: "retry_safe" }))).toBe(
      "Wait again",
    );
    expect(decisionText(resolved(loop, { kind: "retry_safe" }))).toBe(
      "Retry the release",
    );
  });

  it("names the close of an incident an earlier decision left as it was offered", () => {
    // The platform ran nothing again: no retry is named.
    for (const incident of [called, caller, loop])
      expect(
        decisionText(
          resolved(incident, { kind: "retry_safe", leftover: true }),
        ),
      ).toBe("Close incident");
    // A stop or Force end that closed one still ended the run.
    expect(
      decisionText(resolved(called, { kind: "terminate", leftover: true })),
    ).toBe("Force end");
  });

  it("names the other decisions of a transaction as they are offered", () => {
    const said = (
      incident: IncidentView,
      resolution: Partial<NonNullable<IncidentView["resolution"]>>,
    ) => decisionText(resolved(incident, resolution));
    expect(said(called, { kind: "terminate" })).toBe("Force end");
    expect(said(unknown, { kind: "accept_reconciled_failure" })).toBe(
      "It didn't happen",
    );
    expect(
      said(unknown, {
        kind: "accept_reconciled_result",
        output: { as: "compensated" },
      }),
    ).toBe("It happened");
    expect(
      said(called, {
        kind: "accept_reconciled_result",
        output: { as: "compensated", participants: [] },
      }),
    ).toBe("Mark as reconciled as undone");
    expect(
      said(called, {
        kind: "accept_reconciled_result",
        output: { as: "completed", participants: [], blockOutput: {} },
      }),
    ).toBe("Mark as reconciled as finished");
    expect(said(called, { kind: "accept_reconciled_result" })).toBe(
      "Mark as reconciled",
    );
  });

  it("keeps an incident with no transaction in its own words", () => {
    expect(decisionText(resolved(others[0], { kind: "retry_safe" }))).toBe(
      "Retry the step",
    );
    expect(decisionText(resolved(others[0], { kind: "terminate" }))).toBe(
      "End the run",
    );
    expect(
      decisionText(resolved(others[0], { kind: "accept_reconciled_result" })),
    ).toBe("Accept a verified result");
  });
});
