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
import type { Json, JsonObject } from "../src/app/forms/core/json";
import {
  eventProblem,
  keptInput,
  pastedEvent,
  replacedNote,
  SAMPLE_FIELD_PROBLEM,
  withSampleIds,
} from "../src/app/editor/triggers/event-sample";
import {
  eventFieldView,
  sampleEvent,
  type PlatformEventType,
} from "../src/app/editor/triggers/platform-event";
import { available, platformTakes, platformView } from "./platform-view";

const SAMPLE_ID = /^00000000-0000-4000-8000-[0-9a-f]{12}$/;
const REAL_ID = "5f0c2b7e-1111-4222-8333-944455556666";
const event = (type: PlatformEventType = "transaction.in_doubt") =>
  sampleEvent(type, {
    source: "sync-records",
    now: new Date("2026-11-20T10:00:00Z"),
    id: () => REAL_ID,
  }) as JsonObject & { transaction: JsonObject };
describe("the IDs of an event entered as test data", () => {
  it("become sample IDs, each its own, and the rest stays as it was", () => {
    let number = 0;
    const real = sampleEvent("transaction.in_doubt", {
      now: new Date("2026-11-20T10:00:00Z"),
      id: () => `5f0c2b7e-1111-4222-8333-00000000000${++number}`,
    }) as JsonObject & { transaction: JsonObject };
    const { value, replaced } = withSampleIds(real);
    const facts = value.transaction;
    expect(replaced).toEqual(["event", "run", "incident"]);
    const ids = [value["event_id"], facts["run_id"], facts["incident_id"]];
    for (const id of ids) expect(id).toMatch(SAMPLE_ID);
    expect(new Set(ids).size).toBe(3);
    expect(value).toEqual({
      ...real,
      event_id: ids[0],
      transaction: {
        ...real.transaction,
        run_id: ids[1],
        incident_id: ids[2],
      },
    });
  });

  it("become the same sample ID wherever the same real one is, and each time while the samples last", () => {
    const samples = new Map<string, string>();
    const { value } = withSampleIds([event(), event()], samples);
    expect(value[0]).toEqual(value[1]);
    expect(withSampleIds(event(), samples).value).toEqual(value[0]);
    const other = { ...event(), event_id: REAL_ID.replace("5f0c", "6f0c") };
    const next = withSampleIds(other, samples).value;
    expect(next["event_id"]).not.toBe(value[0]!["event_id"]);
    expect(next.transaction).toEqual(value[0]!.transaction);
    expect(withSampleIds(event()).value).not.toEqual(value[0]);
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

  it("become the same sample ID in capitals as in lower case", () => {
    const samples = new Map<string, string>();
    const { value } = withSampleIds(event(), samples);
    const shouted = { ...event(), event_id: REAL_ID.toUpperCase() };
    expect(withSampleIds(shouted, samples).value).toEqual(value);
  });

  it("stay empty while a person empties one: there is no real ID to replace", () => {
    const emptied = { ...event(), event_id: "" };
    const { value, replaced } = withSampleIds(emptied);
    expect(value["event_id"]).toBe("");
    expect(replaced).toEqual(["run", "incident"]);
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

  const { version: _version, ...unversioned } = event();
  it.each<[string, Json, (value: Json) => Json]>([
    [
      "in a field",
      { event: event() },
      (value) => (value as JsonObject)["event"]!,
    ],
    [
      "two levels deep",
      { body: { event: event(), note: "x" } },
      (value) =>
        ((value as JsonObject)["body"] as JsonObject)["event"] as JsonObject,
    ],
    ["in a list", [{ note: "x" }, event()], (value) => (value as Json[])[1]!],
    ["without its version", unversioned, (value) => value],
    [
      "with another version",
      { ...event(), version: "weave/platform-event-v1 " },
      (value) => value,
    ],
  ])("are replaced in an event %s", (_where, entered, at) => {
    const { value, replaced } = withSampleIds<Json>(entered);
    expect(replaced).toEqual(["event", "run", "incident"]);
    expect(JSON.stringify(value)).not.toContain(REAL_ID);
    const held = at(value) as JsonObject;
    const facts = held["transaction"] as JsonObject;
    for (const id of [held["event_id"], facts["run_id"], facts["incident_id"]])
      expect(id).toMatch(SAMPLE_ID);
    expect(facts["business_key"]).toBe("REF-1029");
  });

  it("are replaced in every event a list holds, each kind named once", () => {
    const { value, replaced } = withSampleIds([
      event("transaction.compensated"),
      event(),
    ]);
    expect(replaced).toEqual(["event", "run", "incident"]);
    expect(JSON.stringify(value)).not.toContain(REAL_ID);
  });

  it("are replaced in an event that lists its transactions, and in an event it holds", () => {
    const { value, replaced } = withSampleIds({
      ...unversioned,
      transaction: [unversioned["transaction"] as Json],
      earlier: { items: [event("transaction.compensated")] },
    });
    expect(replaced).toEqual(["event", "run", "incident"]);
    expect(JSON.stringify(value)).not.toContain(REAL_ID);
  });

  it("are no event's without a run, a state and an instance key: the rest of a value stays", () => {
    const facts = event().transaction;
    for (const key of ["run_id", "state", "instance_key"]) {
      const { [key]: _missing, ...some } = facts;
      const value = { event_id: REAL_ID, transaction: some as JsonObject };
      expect(withSampleIds(value)).toEqual({ value, replaced: [] });
    }
    const value = {
      event_id: REAL_ID,
      run_id: REAL_ID,
      items: [null, 1, "x", { transaction: "T-1" }],
    };
    expect(withSampleIds(value)).toEqual({ value, replaced: [] });
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

/** An event with one field changed, or removed for undefined. */
const changed = (path: string, value: unknown): JsonObject => {
  const next = structuredClone(event()) as JsonObject;
  const keys = path.split(".");
  const last = keys.pop()!;
  let node: JsonObject = next;
  for (const key of keys) node = node[key] as JsonObject;
  if (value === undefined) delete node[last];
  else node[last] = value as never;
  return next;
};
const step = { step: "hold", instance_key: "hold", how: "reserve" };
const steps = (done: unknown) => ({ done, undone: [], pending: [] });
const ALL_TYPES = [
  "transaction.completed",
  "transaction.compensated",
  "transaction.cancelled",
  "transaction.in_doubt",
  "transaction.reconciled",
  "transaction.force_ended",
] as const;

/** What is wrong, the event as pasted, and what Studio says until the host serves the fields, when that differs. */
type Refused = [problem: string, event: JsonObject, unread?: string | null];
const ID = "such as 3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e.";
const REFUSED: Refused[] = [
  [
    "version must be weave/platform-event-v1.",
    changed("version", "weave/platform-event-v2"),
  ],
  [
    "type must be one of transaction.completed, transaction.compensated, transaction.cancelled, transaction.in_doubt, transaction.reconciled, transaction.force_ended.",
    changed("type", "transaction.open"),
  ],
  [`event_id must be an ID, ${ID}`, changed("event_id", undefined)],
  [`event_id must be an ID, ${ID}`, changed("event_id", "evt-1")],
  [
    "emitted_at must be a date and time, such as 2026-11-18T16:05:11Z.",
    changed("emitted_at", 5),
  ],
  [
    "emitted_at must be a date and time, such as 2026-11-18T16:05:11Z.",
    changed("emitted_at", "2026"),
  ],
  [
    // No time zone.
    "emitted_at must be a date and time, such as 2026-11-18T16:05:11Z.",
    changed("emitted_at", "2026-11-18T16:05:11"),
  ],
  [
    // No such day.
    "emitted_at must be a date and time, such as 2026-11-18T16:05:11Z.",
    changed("emitted_at", "2026-02-30T16:05:11Z"),
  ],
  [
    // No such hour: the next day's 0.
    "emitted_at must be a date and time, such as 2026-11-18T16:05:11Z.",
    changed("emitted_at", "2026-11-18T24:00:00Z"),
  ],
  [
    // No such year.
    "emitted_at must be a date and time, such as 2026-11-18T16:05:11Z.",
    changed("emitted_at", "0000-11-18T16:05:11Z"),
  ],
  ["transaction must be a group of fields.", changed("transaction", null)],
  [
    "transaction.workflow must be a group of fields.",
    changed("transaction.workflow", undefined),
  ],
  [
    "transaction.workflow.name must be a workflow name, such as move-between-systems.",
    changed("transaction.workflow.name", "sync records"),
    null,
  ],
  [
    "transaction.workflow.version must be a version, such as 1.0.0.",
    changed("transaction.workflow.version", "1"),
    null,
  ],
  [`transaction.run_id must be an ID, ${ID}`, changed("transaction.run_id", 7)],
  [
    "transaction.instance_key must be text.",
    changed("transaction.instance_key", null),
  ],
  [
    "transaction.instance_key must be text.",
    changed("transaction.instance_key", ""),
  ],
  [
    "transaction.instance_key can have up to 512 characters.",
    changed("transaction.instance_key", "x".repeat(513)),
    null,
  ],
  [
    "transaction.business_key must be text or null.",
    changed("transaction.business_key", 12),
  ],
  [
    "transaction.business_key must be text or null.",
    changed("transaction.business_key", undefined),
  ],
  [
    "transaction.business_key can have up to 200 characters.",
    changed("transaction.business_key", "x".repeat(300)),
    null,
  ],
  [
    // A character cut in half, its first part or its second.
    "transaction.business_key has a broken character, such as half an emoji.",
    changed("transaction.business_key", "REF-\ud83d"),
  ],
  [
    "transaction.instance_key has a broken character, such as half an emoji.",
    changed("transaction.instance_key", "\ude00move"),
  ],
  [
    "transaction.steps.done[0].step must be a step ID, such as add.",
    changed("transaction.steps", steps([{ ...step, step: "\ud83d" }])),
    "transaction.steps.done[0].step has a broken character, such as half an emoji.",
  ],
  [
    "transaction.cause must be a group of fields or null.",
    changed("transaction.cause", "x"),
  ],
  [
    "transaction.cause.code must be a code, such as WV-TXN-UNDO-STUCK.",
    changed("transaction.cause", { code: "stuck" }),
    null,
  ],
  [
    "transaction.cause.code must be a code, such as WV-TXN-UNDO-STUCK.",
    changed("transaction.cause", { step: "add" }),
    "transaction.cause.code must be text.",
  ],
  [
    "transaction.incident_id must be an ID or null, such as 3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e.",
    changed("transaction.incident_id", "INC-1"),
  ],
  [
    "transaction.steps must be a group of fields.",
    changed("transaction.steps", []),
  ],
  [
    "transaction.steps.done must be a list.",
    changed("transaction.steps", { done: "no" }),
  ],
  [
    "transaction.steps.done[0].how must be one of reserve, compensate, irreversible.",
    changed("transaction.steps", steps([{ ...step, how: "maybe" }])),
    null,
  ],
  [
    "transaction.steps.done[1].attempts must be 0 or more.",
    changed("transaction.steps", steps([step, { ...step, attempts: -1 }])),
    null,
  ],
  [
    "transaction.steps.done[0].attempts must be a whole number or null.",
    changed("transaction.steps", steps([{ ...step, attempts: 1.5 }])),
  ],
  [
    "transaction.steps.done can have up to 1000 items.",
    changed("transaction.steps", steps(Array(1001).fill(step))),
    null,
  ],
  [
    "transaction.test must be true or false.",
    changed("transaction.test", "no"),
  ],
];
/** A real ID in a field the platform event hasn't, at each level. */
const UNKNOWN: [path: string, event: JsonObject][] = [
  ["root_event_id", changed("root_event_id", REAL_ID)],
  ["constructor", changed("constructor", REAL_ID)],
  ["transaction.parent_run_id", changed("transaction.parent_run_id", REAL_ID)],
  ["transaction.workflow.id", changed("transaction.workflow.id", REAL_ID)],
  [
    "transaction.cause.incident",
    changed("transaction.cause.incident", REAL_ID),
  ],
  ["transaction.steps.run_id", changed("transaction.steps.run_id", REAL_ID)],
  [
    "transaction.steps.done[0].run_id",
    changed("transaction.steps", steps([{ ...step, run_id: REAL_ID }])),
  ],
];

describe.runIf(available)(
  "what is wrong with an event entered as test data",
  () => {
    const fields = available ? eventFieldView(platformView()) : null;
    const problem = (value: JsonObject) => eventProblem(value, fields);

    it("is nothing for each event Studio builds", () => {
      for (const type of ALL_TYPES) {
        expect(problem(event(type)), type).toBeNull();
        expect(eventProblem(event(type), null), type).toBeNull();
      }
    });

    it.each(REFUSED)("is the first problem by field: %s", (said, value) => {
      expect(problem(value)).toBe(said);
    });

    it.each(REFUSED)(
      "is what Studio can tell while the host doesn't serve the fields: %s",
      (said, value, unread) => {
        expect(eventProblem(value, null)).toBe(
          unread === undefined ? said : unread,
        );
      },
    );

    it.each(UNKNOWN)(
      "is a field the platform event hasn't, whether the host serves the fields or not: %s",
      (path, value) => {
        const said = `${path} isn't one of the platform event's fields.`;
        expect(problem(value)).toBe(said);
        expect(eventProblem(value, null)).toBe(said);
      },
    );

    it("is what the platform refuses too, and it takes each event Studio builds", () => {
      const refused = [...REFUSED, ...UNKNOWN].map(([, value]) => value);
      const taken = platformTakes([...refused, ...ALL_TYPES.map(event)]);
      expect(refused.filter((_, index) => taken[index]).map(problem)).toEqual(
        [],
      );
      expect(taken.slice(refused.length)).toEqual(ALL_TYPES.map(() => true));
    });

    it("is nothing for what the platform leaves out or takes empty", () => {
      const events = [
        changed("transaction.cause", undefined),
        changed("transaction.cause", null),
        changed("transaction.incident_id", null),
        changed("transaction.business_key", null),
        changed("transaction.steps", {}),
        changed("transaction.steps", steps([{ ...step, phase: null }])),
        changed("transaction.cause", { code: "WV-X", origin_code: null }),
        changed("emitted_at", "2026-11-18T16:05:11.123456+02:00"),
        changed("emitted_at", "0001-11-18T23:05:11Z"),
        // Counted as the platform counts: 200 characters, 400 in UTF-16.
        changed("transaction.business_key", "𝄞".repeat(200)),
      ];
      expect(platformTakes(events)).toEqual(events.map(() => true));
      expect(events.map(problem)).toEqual(events.map(() => null));
      expect(events.map((value) => eventProblem(value, null))).toEqual(
        events.map(() => null),
      );
    });

    it("knows every field the platform event has, and replaces every ID it holds", () => {
      const all = changed(
        "transaction.steps",
        Object.fromEntries(
          ["done", "undone", "pending"].map((key) => [
            key,
            [{ ...step, phase: "undo", attempts: 1 }],
          ]),
        ),
      );
      const facts = all["transaction"] as JsonObject;
      facts["cause"] = { code: "WV-X", step: "add", origin_code: "FAILED" };
      expect(platformTakes([all])).toEqual([true]);
      expect([problem(all), eventProblem(all, null)]).toEqual([null, null]);
      // The platform's fields, and those that hold an ID, by their paths.
      const paths: string[] = [];
      const ids: string[] = [];
      const visit = (schema: JsonObject, path: string) => {
        if (schema["format"] === "uuid") ids.push(path);
        for (const part of (schema["anyOf"] ?? []) as JsonObject[])
          visit(part, path);
        if (schema["items"]) visit(schema["items"] as JsonObject, path);
        for (const [key, field] of Object.entries(schema["properties"] ?? {})) {
          paths.push(path ? `${path}.${key}` : key);
          visit(field as JsonObject, path ? `${path}.${key}` : key);
        }
      };
      visit(platformView(), "");
      const held: string[] = [];
      const walk = (value: unknown, path: string) => {
        if (Array.isArray(value))
          return value.forEach((item) => walk(item, path));
        if (!value || typeof value !== "object") return;
        for (const [key, item] of Object.entries(value)) {
          held.push(path ? `${path}.${key}` : key);
          walk(item, path ? `${path}.${key}` : key);
        }
      };
      walk(all, "");
      expect([...new Set(held)].sort()).toEqual(paths.sort());
      const { value } = withSampleIds(all);
      const at = (path: string) =>
        path
          .split(".")
          .reduce<unknown>((node, key) => (node as JsonObject)[key], value);
      expect(ids).toHaveLength(3);
      for (const path of ids) expect(at(path), path).toMatch(SAMPLE_ID);
    });

    it("is a state that isn't the event's, which no event the platform sends has", () => {
      for (const known of [fields, null]) {
        expect(eventProblem(changed("transaction.state", "open"), known)).toBe(
          "transaction.state must be one of in_doubt, compensated, cancelled, completed, reconciled, force_ended.",
        );
        expect(
          eventProblem(changed("transaction.state", "completed"), known),
        ).toBe("transaction.state must be in_doubt for transaction.in_doubt.");
      }
    });

    it("is told by the fields Studio knows when the host answers something else", () => {
      expect(eventProblem(event(), { features: [] })).toBeNull();
      expect(
        eventProblem(changed("root_event_id", REAL_ID), { features: [] }),
      ).toBe("root_event_id isn't one of the platform event's fields.");
    });
  },
);

describe("what test data keeps of an event in an input with secret fields", () => {
  const { version: _version, ...unversioned } = event();
  const secret = { type: "string", "x-secret": true };
  const facts = (properties: JsonObject): JsonObject => ({
    type: "object",
    properties,
  });
  it.each<[string, string[], JsonObject, JsonObject]>([
    [
      "its version and its state",
      ["transaction", "state"],
      { version: secret, transaction: facts({ state: secret }) },
      event(),
    ],
    [
      "its version, and its transaction can't be read",
      ["transaction"],
      { version: secret, transaction: { $ref: "shared.missing-v1" } },
      event(),
    ],
    [
      "its instance key, without a version",
      ["transaction", "instance_key"],
      { transaction: facts({ instance_key: secret }) },
      unversioned,
    ],
  ])(
    "keeps no real ID when the input hides %s",
    (_what, hidden, properties, value) => {
      const kept = keptInput(
        { type: "object", properties },
        value,
        () => null,
        new Map(),
      );
      const text = JSON.stringify(kept);
      expect(text).not.toContain(REAL_ID);
      expect(text).toContain("00000000-0000-4000-8000-");
      // What the input hides stays hidden.
      expect(kept).toHaveProperty("value.event_id");
      expect(kept).not.toHaveProperty(["value", ...hidden]);
    },
  );
});

describe("a pasted event", () => {
  it("is refused when the text isn't a JSON object", () => {
    for (const source of ["{oops", "[1]", "null", '"text"'])
      expect(pastedEvent(source, null)).toEqual({
        problem: SAMPLE_FIELD_PROBLEM,
      });
  });

  it("keeps no field the platform event hasn't, one named __proto__ too", () => {
    const text = JSON.stringify(event()).replace(
      "{",
      `{"__proto__":{"run_id":"${REAL_ID}"},`,
    );
    expect(pastedEvent(text, null)).toEqual({
      problem: "__proto__ isn't one of the platform event's fields.",
    });
  });

  it("is read as it was pasted, or refused with its first problem", () => {
    expect(pastedEvent(JSON.stringify(event()), null)).toEqual({
      value: event(),
    });
    expect(
      pastedEvent(JSON.stringify(changed("transaction", null)), null),
    ).toEqual({ problem: "transaction must be a group of fields." });
  });
});
