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
  pastedEvent,
  replacedNote,
  SAMPLE_FIELD_PROBLEM,
} from "../src/app/editor/triggers/event-sample";
import {
  sampleEvent,
  type PlatformEventType,
} from "../src/app/editor/triggers/platform-event";

let counter = 0;
const ids = () =>
  `00000000-0000-4000-8000-${String(++counter).padStart(12, "0")}`;
const event = (type: PlatformEventType = "transaction.in_doubt") =>
  sampleEvent(type, {
    source: "sync-records",
    now: new Date("2026-11-20T10:00:00Z"),
    id: () => "5f0c2b7e-1111-4222-8333-944455556666",
  }) as {
    event_id: string;
    transaction: Record<string, unknown>;
  } & Record<string, unknown>;
const read = (value: unknown) => pastedEvent(JSON.stringify(value), ids);
const problemOf = (value: unknown) => {
  const answer = read(value);
  return "problem" in answer ? answer.problem : null;
};

describe("a pasted event", () => {
  it("gets sample IDs in place of the event, run and incident IDs", () => {
    const answer = read(event());
    if ("problem" in answer) throw Error(answer.problem);
    const pasted = answer.value as ReturnType<typeof event>;
    expect(answer.replaced).toEqual(["event", "run", "incident"]);
    for (const id of [
      pasted.event_id,
      pasted.transaction["run_id"],
      pasted.transaction["incident_id"],
    ]) {
      expect(id).toMatch(/^00000000-0000-4000-8000-/);
      expect(id).not.toBe("5f0c2b7e-1111-4222-8333-944455556666");
    }
    expect(
      new Set([
        pasted.event_id,
        pasted.transaction["run_id"],
        pasted.transaction["incident_id"],
      ]).size,
    ).toBe(3);
    // Everything else is the event as pasted.
    expect(pasted.transaction["business_key"]).toBe("REF-1029");
    expect(pasted.transaction["workflow"]).toEqual({
      name: "sync-records",
      version: "1.0.0",
    });
  });

  it("says only the IDs it replaced", () => {
    const answer = read(event("transaction.compensated"));
    if ("problem" in answer) throw Error(answer.problem);
    expect(answer.replaced).toEqual(["event", "run"]);
    expect(
      "incident_id" in (answer.value as ReturnType<typeof event>).transaction,
    ).toBe(false);
    expect(replacedNote(["event", "run"])).toBe(
      "Studio replaced the event and run IDs with sample IDs.",
    );
    expect(replacedNote(["event", "run", "incident"])).toBe(
      "Studio replaced the event, run and incident IDs with sample IDs.",
    );
    expect(replacedNote(["run"])).toBe(
      "Studio replaced the run ID with a sample ID.",
    );
    expect(replacedNote([])).toBe("");
  });

  it("keeps an incident that is empty as it is", () => {
    const pasted = event("transaction.compensated");
    pasted.transaction["incident_id"] = null;
    const answer = read(pasted);
    if ("problem" in answer) throw Error(answer.problem);
    expect(
      (answer.value as ReturnType<typeof event>).transaction["incident_id"],
    ).toBeNull();
    expect(answer.replaced).toEqual(["event", "run"]);
  });

  it("isn't changed in place", () => {
    const text = JSON.stringify(event());
    pastedEvent(text, ids);
    expect(text).toBe(JSON.stringify(event()));
  });
});

describe("what is wrong with a pasted event, the first problem by field", () => {
  const changed = (
    path: [string] | [string, string] | [string, string, string],
    value: unknown,
  ) => {
    const next = structuredClone(event()) as Record<string, unknown>;
    const parents = path.slice(0, -1);
    let node = next;
    for (const key of parents) node = node[key] as Record<string, unknown>;
    const last = path.at(-1)!;
    if (value === undefined) delete node[last];
    else node[last] = value;
    return next;
  };

  it("is nothing for each event Studio builds", () => {
    for (const type of [
      "transaction.completed",
      "transaction.compensated",
      "transaction.cancelled",
      "transaction.in_doubt",
      "transaction.reconciled",
      "transaction.force_ended",
    ] as const)
      expect(problemOf(event(type)), type).toBeNull();
  });

  it("is the text, when it isn't a JSON object", () => {
    const notJson = pastedEvent("{oops", ids);
    expect(notJson).toEqual({ problem: SAMPLE_FIELD_PROBLEM });
    expect(problemOf([1])).toBe(SAMPLE_FIELD_PROBLEM);
  });

  it.each<[string, Record<string, unknown>, string]>([
    [
      "version",
      changed(["version"], "weave/platform-event-v2"),
      "version must be weave/platform-event-v1.",
    ],
    [
      "type",
      changed(["type"], "transaction.open"),
      "type must be one of transaction.completed, transaction.compensated, transaction.cancelled, transaction.in_doubt, transaction.reconciled, transaction.force_ended.",
    ],
    [
      "emitted_at",
      changed(["emitted_at"], 5),
      "emitted_at must be a date and time.",
    ],
    [
      "no transaction",
      changed(["transaction"], null),
      "transaction must be a group of fields.",
    ],
    [
      "no workflow",
      changed(["transaction", "workflow"], undefined),
      "transaction.workflow must be a group of fields.",
    ],
    [
      "workflow name",
      changed(["transaction", "workflow", "name"], ""),
      "transaction.workflow.name must be text.",
    ],
    [
      "instance key",
      changed(["transaction", "instance_key"], null),
      "transaction.instance_key must be text.",
    ],
    [
      "business key",
      changed(["transaction", "business_key"], 12),
      "transaction.business_key must be text or null.",
    ],
    [
      "state",
      changed(["transaction", "state"], "open"),
      "transaction.state must be one of in_doubt, compensated, cancelled, completed, reconciled, force_ended.",
    ],
    [
      "state of another event",
      changed(["transaction", "state"], "completed"),
      "transaction.state must be in_doubt for transaction.in_doubt.",
    ],
    [
      "steps",
      changed(["transaction", "steps"], []),
      "transaction.steps must be a group of fields.",
    ],
    [
      "test",
      changed(["transaction", "test"], "no"),
      "transaction.test must be true or false.",
    ],
  ])("names %s", (_name, value, problem) => {
    expect(problemOf(value)).toBe(problem);
  });
});
