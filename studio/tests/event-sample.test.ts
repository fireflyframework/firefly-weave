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
import type { JsonObject } from "../src/app/forms/core/json";
import {
  pastedEvent,
  replacedNote,
  SAMPLE_FIELD_PROBLEM,
  withSampleIds,
} from "../src/app/editor/triggers/event-sample";
import {
  sampleEvent,
  type PlatformEventType,
} from "../src/app/editor/triggers/platform-event";

const SAMPLE_ID = /^00000000-0000-4000-8000-[0-9a-f]{12}$/;
const REAL_ID = "5f0c2b7e-1111-4222-8333-944455556666";
const event = (type: PlatformEventType = "transaction.in_doubt") =>
  sampleEvent(type, {
    source: "sync-records",
    now: new Date("2026-11-20T10:00:00Z"),
    id: () => REAL_ID,
  }) as JsonObject & { transaction: JsonObject };
const problemOf = (value: unknown) => {
  const answer = pastedEvent(JSON.stringify(value));
  return "problem" in answer ? answer.problem : null;
};

describe("the IDs of an event entered as test data", () => {
  it("become sample IDs, each its own, and the rest stays as it was", () => {
    const { value, replaced } = withSampleIds(event());
    const facts = value["transaction"] as JsonObject;
    expect(replaced).toEqual(["event", "run", "incident"]);
    const ids = [value["event_id"], facts["run_id"], facts["incident_id"]];
    for (const id of ids) expect(id).toMatch(SAMPLE_ID);
    expect(new Set(ids).size).toBe(3);
    expect(value).toEqual({
      ...event(),
      event_id: ids[0],
      transaction: {
        ...event().transaction,
        run_id: ids[1],
        incident_id: ids[2],
      },
    });
  });

  it("stay when they are sample IDs already", () => {
    const { value } = withSampleIds(event());
    expect(withSampleIds(value)).toEqual({ value, replaced: [] });
  });

  it("are replaced in whatever spelling: without hyphens, in capitals, or not as text", () => {
    for (const id of [
      REAL_ID.replaceAll("-", ""),
      REAL_ID.toUpperCase(),
      { id: REAL_ID },
      `00000000-0000-4000-8000-000000000001 ${REAL_ID}`,
    ]) {
      const { value, replaced } = withSampleIds({ ...event(), event_id: id });
      expect(replaced).toContain("event");
      expect(value["event_id"]).toMatch(SAMPLE_ID);
    }
  });

  it("are only those it has: an incident that is missing or empty stays so", () => {
    const undone = event("transaction.compensated");
    const without = withSampleIds(undone);
    expect(without.replaced).toEqual(["event", "run"]);
    expect("incident_id" in (without.value["transaction"] as object)).toBe(
      false,
    );
    const empty = withSampleIds({
      ...undone,
      transaction: { ...undone.transaction, incident_id: null },
    });
    expect(empty.replaced).toEqual(["event", "run"]);
    expect((empty.value["transaction"] as JsonObject)["incident_id"]).toBe(
      null,
    );
  });

  it("are named in the note, only the ones replaced", () => {
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
    const notJson = pastedEvent("{oops");
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
