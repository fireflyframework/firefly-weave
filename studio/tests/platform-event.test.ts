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
// Platform events in Studio: the platform's event types with Studio's words
// for them, the event's schema read once from the Studio host, and sample
// events of each type. The shared corpus is the platform's side, checked by
// tests/unit/test_platform_event_corpus.py.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { ApiError } from "../src/app/api";
import type { JsonObject } from "../src/app/forms/core/json";
import {
  EVENT_CHOICE_ORDER,
  EVENT_CHOICES,
  EVENT_TYPES,
  PLATFORM_EVENT_INPUT,
  PLATFORM_EVENT_SCHEMA,
  VIEW_RETRY_MS,
  currentPlatformEvent,
  isPlatformEventInput,
  namedSchemas,
  platformEventView,
  sampleEvent,
  type PlatformEventType,
} from "../src/app/editor/triggers/platform-event";

interface Corpus {
  events: { type: PlatformEventType; label: string; short: string }[];
  samples: {
    now: string;
    ids: string[];
    cases: { source?: string; event: JsonObject & { type: string } }[];
  };
}
const corpus = JSON.parse(
  readFileSync(
    resolve(import.meta.dirname, "fixtures/platform-event-corpus.json"),
    "utf8",
  ),
) as Corpus;

const VIEW: JsonObject = {
  type: "object",
  properties: { type: { type: "string" } },
};
const ROUTE = "/studio/contracts/platform-event";

/** The Studio host's contract route: answers in order, then the last one again. */
class FakeHost {
  readonly asked: string[] = [];
  constructor(private readonly answers: (JsonObject | unknown[] | Error)[]) {}
  async request<T>(path: string): Promise<T> {
    this.asked.push(path);
    const answer =
      this.answers.length > 1 ? this.answers.shift() : this.answers[0];
    if (answer instanceof Error) throw answer;
    return answer as T;
  }
}
const unavailable = () =>
  new ApiError(503, {
    code: "WV-STUDIO-UPSTREAM",
    message: "The host is busy.",
  });

describe("platform event types and their words", () => {
  it("lists the platform's event types in its order", () => {
    expect(EVENT_TYPES).toEqual(corpus.events.map((entry) => entry.type));
  });

  it("names each event as the corpus does", () => {
    expect(
      corpus.events.map((entry) => ({
        type: entry.type,
        ...EVENT_CHOICES[entry.type],
      })),
    ).toEqual(corpus.events);
  });

  it("offers the events with the one that needs attention first", () => {
    expect(EVENT_CHOICE_ORDER).toEqual([
      "transaction.in_doubt",
      "transaction.compensated",
      "transaction.cancelled",
      "transaction.completed",
      "transaction.reconciled",
      "transaction.force_ended",
    ]);
  });
});

describe("sample events", () => {
  it.each(corpus.samples.cases)(
    "builds the $event.type sample the platform accepts",
    ({ source, event }) => {
      let next = 0;
      const id = () => corpus.samples.ids[next++]!;
      expect(
        sampleEvent(event.type as PlatformEventType, {
          ...(source === undefined ? {} : { source }),
          now: new Date(corpus.samples.now),
          id,
        }),
      ).toEqual(event);
    },
  );
});

describe("the platform event as a workflow's input", () => {
  it("is exactly the reference to the platform's schema", () => {
    expect(PLATFORM_EVENT_INPUT).toEqual({ $ref: "weave.platform-event-v1" });
    expect(isPlatformEventInput({ $ref: PLATFORM_EVENT_SCHEMA })).toBe(true);
  });

  it.each([
    ["another schema name", { $ref: "weave.platform-event-v2" }],
    [
      "the reference with a title",
      { $ref: PLATFORM_EVENT_SCHEMA, title: "Event" },
    ],
    ["an object schema", { type: "object" }],
    ["the name alone", PLATFORM_EVENT_SCHEMA],
    ["no schema", undefined],
  ])("is not %s", (_name, schema) => {
    expect(isPlatformEventInput(schema)).toBe(false);
  });

  it("resolves the platform's schema name to the view and nothing else", () => {
    const named = namedSchemas(VIEW);
    expect(named(PLATFORM_EVENT_SCHEMA)).toBe(VIEW);
    expect(named("customer.json")).toBeUndefined();
    expect(namedSchemas(null)(PLATFORM_EVENT_SCHEMA)).toBeUndefined();
  });
});

describe("reading the platform event's view from the Studio host", () => {
  it("reads it once per Studio connection", async () => {
    const host = new FakeHost([VIEW]);
    const both = await Promise.all([
      platformEventView(host),
      platformEventView(host),
    ]);
    const later = await platformEventView(host);
    expect(both).toEqual([
      { state: "ready", schema: VIEW },
      { state: "ready", schema: VIEW },
    ]);
    expect(later).toEqual({ state: "ready", schema: VIEW });
    expect(host.asked).toEqual([ROUTE]);
  });

  it("waits 5 s after a failure, doubling up to a minute", async () => {
    const host = new FakeHost([unavailable()]);
    let now = 1_000_000;
    const clock = () => now;
    const pauses: number[] = [];
    for (let attempt = 0; attempt < 6; attempt++) {
      const answer = await platformEventView(host, clock);
      if (answer.state !== "unavailable") throw Error("expected a failure");
      pauses.push(answer.retryAt - now);
      // Asked again before the pause ends: no request, the same time to retry.
      now = answer.retryAt - 1;
      expect(await platformEventView(host, clock)).toEqual(answer);
      now = answer.retryAt;
    }
    expect(pauses).toEqual([5_000, 10_000, 20_000, 40_000, 60_000, 60_000]);
    expect(host.asked).toHaveLength(6);
  });

  it("reads it again after the pause and keeps it", async () => {
    const host = new FakeHost([unavailable(), VIEW]);
    let now = 0;
    const first = await platformEventView(host, () => now);
    now = 5_000;
    expect(first).toEqual({ state: "unavailable", retryAt: 5_000 });
    expect(await platformEventView(host, () => now)).toEqual({
      state: "ready",
      schema: VIEW,
    });
    now = 600_000;
    expect(await platformEventView(host, () => now)).toEqual({
      state: "ready",
      schema: VIEW,
    });
    expect(host.asked).toHaveLength(2);
  });

  it("is unavailable without a Studio session", async () => {
    const host = new FakeHost([
      new ApiError(401, {
        code: "WV-STUDIO-SESSION",
        message: "Pair this browser with Weave Studio.",
      }),
    ]);
    expect(await platformEventView(host, () => 0)).toEqual({
      state: "unavailable",
      retryAt: 5_000,
    });
  });

  it("is unavailable when the host answers something that isn't a schema", async () => {
    const host = new FakeHost([[VIEW]]);
    expect(await platformEventView(host, () => 0)).toEqual({
      state: "unavailable",
      retryAt: 5_000,
    });
  });
});

describe("the view as a template reads it", () => {
  const settled = () => new Promise((done) => setTimeout(done));

  it("is nothing while the first read runs, then the view, asked for once however often it is drawn", async () => {
    const host = new FakeHost([VIEW]);
    expect(currentPlatformEvent(host)).toBeNull();
    expect(currentPlatformEvent(host)).toBeNull();
    await settled();
    expect(currentPlatformEvent(host)).toEqual({
      state: "ready",
      schema: VIEW,
    });
    expect(host.asked).toEqual([ROUTE]);
  });

  it("stays unavailable through the pause, then reads it again", async () => {
    const host = new FakeHost([unavailable(), VIEW]);
    let now = 0;
    currentPlatformEvent(host, () => now);
    await settled();
    now = VIEW_RETRY_MS - 1;
    const paused = currentPlatformEvent(host, () => now);
    expect(paused).toEqual({ state: "unavailable", retryAt: VIEW_RETRY_MS });
    await settled();
    // Drawing it again announces nothing, so it never draws itself in a loop.
    expect(currentPlatformEvent(host, () => now)).toBe(paused);
    expect(host.asked).toHaveLength(1);
    now = VIEW_RETRY_MS;
    // Still unavailable while the second read runs.
    expect(currentPlatformEvent(host, () => now)).toMatchObject({
      state: "unavailable",
    });
    await settled();
    expect(currentPlatformEvent(host, () => now)).toEqual({
      state: "ready",
      schema: VIEW,
    });
    expect(host.asked).toHaveLength(2);
  });

  it("keeps each connection's view apart", async () => {
    const first = new FakeHost([VIEW]);
    currentPlatformEvent(first);
    await settled();
    expect(currentPlatformEvent(new FakeHost([unavailable()]))).toBeNull();
  });
});
