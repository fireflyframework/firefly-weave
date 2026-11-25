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
// Test data loaded from a file is read by everyone who can test the workflow,
// as what a person enters is: its input keeps no ID of a real platform event.
// An input that names the environment of the run it came from keeps its
// value, whatever its file says of its source.
import { describe, expect, it } from "vitest";
import type { Json, JsonObject } from "../src/app/forms/core/json";
import { loadFileTestData } from "../src/app/editor/state/test-data-file";
import { emptyTestData } from "../src/app/editor/state/test-data-ops";
import type {
  InputEntry,
  StudioTestData,
} from "../src/app/editor/state/test-data";
import { sampleEvent } from "../src/app/editor/triggers/platform-event";

const REAL_ID = "5f0c2b7e-1111-4222-8333-944455556666";
const event = () =>
  sampleEvent("transaction.in_doubt", {
    source: "sync-records",
    now: new Date("2026-11-20T10:00:00Z"),
    id: () => REAL_ID,
  }) as JsonObject;
const EVENT_INPUT = { $ref: "weave.platform-event-v1" };
const file = (input?: InputEntry): StudioTestData => ({
  ...emptyTestData("2026-11-20T10:00:00Z"),
  ...(input ? { input } : {}),
});

/** The test data service of the open workflow: what it was asked to keep. */
function service() {
  const kept: { data: StudioTestData; pointers: string[] }[] = [];
  return {
    kept,
    sampleIds: new Map<string, string>(),
    change: async (data: StudioTestData, pointers: string[]) => {
      kept.push({ data, pointers });
      return { ok: true as const, previous: file() };
    },
  };
}
const load = (
  data: StudioTestData,
  schema: unknown = { type: "object" },
  keeping = service(),
) =>
  loadFileTestData(keeping, data, schema, () => null).then((outcome) => ({
    outcome,
    kept: keeping.kept,
  }));

describe("the input of a test data file", () => {
  it.each<[string, Json, InputEntry["source"], unknown]>([
    ["a person's", event(), "manual", EVENT_INPUT],
    [
      "a simulated one, the event inside it",
      { body: { event: event() } },
      "simulated",
      { type: "object" },
    ],
    ["one that says it is a run's", event(), "run", { type: "object" }],
    [
      "one that says it is a test call's",
      [event()],
      "test-call",
      { type: "array" },
    ],
  ])("keeps sample IDs: %s", async (_what, value, source, schema) => {
    const { outcome, kept } = await load(file({ value, source }), schema);
    expect(outcome.ok).toBe(true);
    expect(kept).toHaveLength(1);
    expect(kept[0]!.pointers).toEqual(["/input"]);
    expect(kept[0]!.data.input!.source).toBe(source);
    const text = JSON.stringify(kept);
    expect(text).toContain("REF-1029");
    expect(text).not.toContain(REAL_ID);
  });

  it("gets the sample IDs the open workflow already gave the same event", async () => {
    const keeping = service();
    const data = file({ value: event(), source: "manual" });
    await load(data, EVENT_INPUT, keeping);
    await load(data, EVENT_INPUT, keeping);
    expect(keeping.kept[0]!.data.input).toEqual(keeping.kept[1]!.data.input);
  });

  it("keeps a run's value and where it came from", async () => {
    const input: InputEntry = {
      value: event(),
      source: "run",
      runId: "5a4b1dce-8baa-4cb0-8fa6-9e6a5b7c2d56",
      runEnvironmentId: "d3c29f40-0322-4e38-8b2e-16e2d3f4afde",
    };
    const { kept } = await load(file(input), EVENT_INPUT);
    expect(kept[0]!.data.input).toEqual(input);
  });

  it("isn't loaded with a field a platform event hasn't: the problem says which", async () => {
    const { outcome, kept } = await load(
      file({
        value: { ...event(), root_event_id: REAL_ID },
        source: "manual",
      }),
      EVENT_INPUT,
    );
    expect(outcome).toEqual({
      ok: false,
      message: "root_event_id isn't one of the platform event's fields.",
    });
    expect(kept).toEqual([]);
  });

  it("loses its secret fields, and the input when nothing of it may be kept", async () => {
    const secret = { type: "string", "x-secret": true };
    const { kept } = await load(
      file({ value: { token: "s3cret", id: "o-1" }, source: "manual" }),
      { type: "object", properties: { token: secret } },
    );
    expect(kept[0]!.data.input!.value).toEqual({ id: "o-1" });
    const none = await load(
      file({ value: "s3cret", source: "manual" }),
      secret,
    );
    expect(none.kept[0]).toEqual({ data: file(), pointers: [] });
  });

  it("may be missing: the rest of the file loads, every entry checked", async () => {
    const pin = {
      output: { id: "o-1" },
      source: "manual" as const,
      pinnedAt: "2026-11-20T10:00:00Z",
      configHash: "fnv1a64:0000000000000001" as const,
      contractHash: "fnv1a64:0000000000000002" as const,
      upstreamHash: "fnv1a64:0000000000000003" as const,
    };
    const data = { ...file(), pins: { send: pin } };
    const { kept } = await load(data);
    expect(kept).toEqual([{ data, pointers: ["/pins/send"] }]);
  });
});
