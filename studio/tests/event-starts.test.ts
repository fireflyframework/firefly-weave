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
// What a platform event trigger started: one line per event offered to it,
// with the run it started or why it started none, in the platform's words.
// The list is read page by page; a failed read waits before it is tried
// again, and an answer for an environment or trigger the person left is
// dropped.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import type { TriggerApi } from "../src/app/editor/activation/trigger-resources-api";
import {
  START_COPY,
  readStarts,
  startRow,
  waitingCount,
  type EventStart,
} from "../src/app/editor/activation/event-starts";

const corpus = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/platform-event-corpus.json"),
    "utf8",
  ),
) as { starts: Record<string, string> };

const ENV = "/studio/api/api/v1/tenants/t/projects/p/environments/e";
const RUN = "9b1c4e2a-1d2e-4f3a-8b4c-5d6e7f8a9b0c";
const SOURCE = "3fa2c1d0-6b5e-4d7c-9a8f-2e1d0c9b8a7f";

const start = (fields: Partial<EventStart> = {}): EventStart => ({
  id: "s-1",
  trigger_id: "t-1",
  event_id: "e-1",
  event_type: "transaction.in_doubt",
  source_run_id: SOURCE,
  root_event_id: "e-1",
  depth: 1,
  status: "requested",
  requested_at: "2026-11-20T09:30:00Z",
  ...fields,
});

/** The platform's starts route: answers in order; each answer may also change what the person chose. */
class FakeStarts implements TriggerApi {
  environment = ENV;
  readonly asked: string[] = [];
  constructor(private readonly answers: (unknown | (() => unknown))[]) {}
  async page(): Promise<never> {
    throw Error("Starts are read with explicit paths.");
  }
  async mutate(): Promise<never> {
    throw Error("Reading starts changes nothing.");
  }
  async request<T>(path: string): Promise<T> {
    this.asked.push(path);
    const next = this.answers.shift();
    const answer = typeof next === "function" ? next() : next;
    if (answer instanceof Error) throw answer;
    return answer as T;
  }
}
const busy = () =>
  new ApiError(503, { code: "WV-UNAVAILABLE", message: "Try again later." });

describe("the platform's words for what a trigger started", () => {
  it("are the platform's own sentences", () => {
    expect(START_COPY).toEqual(corpus.starts);
  });

  it("names the run a start started and the run whose event it was", () => {
    expect(
      startRow(
        start({
          status: "started",
          run_id: RUN,
          started_at: "2026-11-20T09:30:02Z",
        }),
      ),
    ).toEqual({
      id: "s-1",
      tone: "ok",
      title: "Started run 9b1c4e2a",
      detail: "Needs attention in run 3fa2c1d0",
      runId: RUN,
      sourceRunId: SOURCE,
      at: "2026-11-20T09:30:02Z",
    });
  });

  it("says a start is waiting", () => {
    expect(startRow(start({ event_type: "transaction.compensated" }))).toEqual({
      id: "s-1",
      tone: "waiting",
      title: "Waiting to start",
      detail: "Undone in run 3fa2c1d0",
      sourceRunId: SOURCE,
      at: "2026-11-20T09:30:00Z",
    });
  });

  it.each([
    "WV-TRIGGER-DISABLED",
    "WV-TRIGGER-EVENT-DEPTH",
    "WV-TRIGGER-CHAIN-BUDGET",
  ] as const)("says why %s skipped a start", (code) => {
    expect(startRow(start({ status: "skipped", code }))).toMatchObject({
      tone: "skipped",
      title: corpus.starts[code],
      detail: "Needs attention in run 3fa2c1d0",
    });
  });

  it.each([
    [
      "WV-FORBIDDEN",
      "The triggered workflow couldn't start. Its owner can no longer start or read runs here.",
    ],
    [
      "WV-READINESS",
      "The triggered workflow couldn't start. Its version can't run here yet: a worker or connection it needs isn't ready.",
    ],
    [
      "WV-RUNTIME-INPUT",
      "The triggered workflow couldn't start. The event it should start with is missing or isn't a platform event.",
    ],
    [
      "WV-RUNTIME-LIMIT",
      "The triggered workflow couldn't start. (WV-RUNTIME-LIMIT)",
    ],
  ])("says why a start was rejected for %s", (cause, title) => {
    expect(
      startRow(start({ status: "rejected", code: "WV-TRIGGER-START", cause })),
    ).toMatchObject({ tone: "failed", title });
  });

  it("says a start expired without a cause", () => {
    expect(
      startRow(start({ status: "rejected", code: "WV-TRIGGER-START-EXPIRED" })),
    ).toMatchObject({
      tone: "failed",
      title:
        "The workflow couldn't start within a day of the event, so the event no longer starts it.",
    });
  });

  it("shows a code or an event type it doesn't know as it is", () => {
    expect(
      startRow(
        start({
          status: "skipped",
          code: "WV-TRIGGER-NEW" as never,
          event_type: "transaction.paused" as never,
        }),
      ),
    ).toMatchObject({
      title: "WV-TRIGGER-NEW",
      detail: "transaction.paused in run 3fa2c1d0",
    });
  });

  it("counts the starts still waiting", () => {
    expect(
      waitingCount(
        [
          start(),
          start({ status: "started", run_id: RUN }),
          start({ id: "s-3" }),
          start({ status: "skipped", code: "WV-TRIGGER-DISABLED" }),
        ].map(startRow),
      ),
    ).toBe(2);
  });
});

describe("reading what a trigger started", () => {
  it("reads page by page with the cursor", async () => {
    const api = new FakeStarts([
      { items: [start(), start({ id: "s-2" })], next_cursor: "c-2" },
      { items: [start({ id: "s-3" })], next_cursor: null },
    ]);
    const first = await readStarts(api, "t-1");
    expect(first).toMatchObject({ next: "c-2" });
    expect("rows" in first && first.rows.map((row) => row.id)).toEqual([
      "s-1",
      "s-2",
    ]);
    const second = await readStarts(api, "t-1", "c-2");
    expect(second).toMatchObject({ rows: [{ id: "s-3" }], next: null });
    expect(api.asked).toEqual([
      `${ENV}/event-triggers/t-1/starts?limit=50`,
      `${ENV}/event-triggers/t-1/starts?limit=50&cursor=c-2`,
    ]);
  });

  it.each([
    [403, "WV-FORBIDDEN", "forbidden"],
    [404, "WV-NOT-FOUND", "missing"],
  ])("says %i %s is %s", async (status, code, refused) => {
    const api = new FakeStarts([
      new ApiError(status, { code, message: "Refused" }),
    ]);
    expect(await readStarts(api, "t-1")).toEqual({ refused });
  });

  it("waits 5 s after a failure, doubling, and starts over after an answer", async () => {
    const api = new FakeStarts([
      busy(),
      busy(),
      { items: [], next_cursor: null },
      busy(),
    ]);
    let now = 1_000;
    const clock = { now: () => now };
    expect(await readStarts(api, "t-1", undefined, clock)).toMatchObject({
      retryAt: 6_000,
    });
    now = 5_999;
    expect(await readStarts(api, "t-1", undefined, clock)).toMatchObject({
      retryAt: 6_000,
    });
    expect(api.asked).toHaveLength(1);
    now = 6_000;
    expect(await readStarts(api, "t-1", undefined, clock)).toMatchObject({
      retryAt: 16_000,
    });
    now = 16_000;
    expect(await readStarts(api, "t-1", undefined, clock)).toEqual({
      rows: [],
      next: null,
    });
    expect(await readStarts(api, "t-1", undefined, clock)).toMatchObject({
      retryAt: 21_000,
    });
    expect(api.asked).toHaveLength(4);
  });

  it("waits for each trigger on its own", async () => {
    const api = new FakeStarts([busy(), { items: [], next_cursor: null }]);
    const clock = { now: () => 0 };
    await readStarts(api, "t-1", undefined, clock);
    expect(await readStarts(api, "t-2", undefined, clock)).toEqual({
      rows: [],
      next: null,
    });
  });

  it("drops an answer when the person chose another environment meanwhile", async () => {
    const api = new FakeStarts([
      () => {
        api.environment = `${ENV}-elsewhere`;
        return { items: [start()], next_cursor: null };
      },
    ]);
    expect(await readStarts(api, "t-1")).toEqual({ dropped: true });
  });

  it("drops an answer when the step details show another trigger meanwhile", async () => {
    let shown = "t-1";
    const api = new FakeStarts([
      () => {
        shown = "t-2";
        return { items: [start()], next_cursor: null };
      },
    ]);
    expect(
      await readStarts(api, "t-1", undefined, { current: () => shown }),
    ).toEqual({ dropped: true });
  });
});
