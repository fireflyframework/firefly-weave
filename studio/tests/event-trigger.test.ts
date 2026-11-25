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
// A workflow declares the platform events that may start it in
// `spec.eventTrigger`: which events, and from which workflows (any workflow
// in the project when `from` is left out). Studio reads and writes that
// declaration, describes it on the tile, says what is wrong with it, and
// decides whether the workflow's input already takes the platform event.
import { describe, expect, it } from "vitest";
import type { Workflow } from "../src/app/model";
import {
  DEFAULT_DECLARATION,
  eventInputFit,
  eventProblems,
  eventSubtitle,
  eventTileName,
  eventTileSubtitle,
  loopWarning,
  readDeclaration,
  withDeclaration,
  type EventDeclaration,
} from "../src/app/editor/triggers/event-trigger";

const workflow = (spec: Record<string, unknown> = {}): Workflow => ({
  apiVersion: "weave/v1alpha1",
  kind: "Workflow",
  metadata: { name: "notify-team", version: "1.0.0" },
  spec: {
    inputSchema: { $ref: "weave.platform-event-v1" },
    outputSchema: {},
    steps: [{ id: "key", kind: "transform", value: { ref: "/input/type" } }],
    output: { literal: null },
    ...spec,
  },
});
const ATTENTION: EventDeclaration = {
  events: ["transaction.in_doubt"],
  from: ["move-between-systems"],
};

describe("reading the declaration", () => {
  it("is null when the workflow doesn't react to platform events", () => {
    expect(readDeclaration(workflow())).toBeNull();
  });

  it("reads the events and any workflow when from is left out", () => {
    expect(
      readDeclaration(
        workflow({ eventTrigger: { events: ["transaction.in_doubt"] } }),
      ),
    ).toEqual({ events: ["transaction.in_doubt"], from: null });
  });

  it("reads the source workflows from from", () => {
    expect(
      readDeclaration(
        workflow({
          eventTrigger: {
            events: ["transaction.compensated", "transaction.in_doubt"],
            from: ["move-between-systems", "sync-references"],
          },
        }),
      ),
    ).toEqual({
      events: ["transaction.compensated", "transaction.in_doubt"],
      from: ["move-between-systems", "sync-references"],
    });
  });

  it("never reads a from that isn't a list as any workflow", () => {
    expect(
      readDeclaration(
        workflow({
          eventTrigger: {
            events: ["transaction.in_doubt"],
            from: "move-between-systems",
          },
        }),
      ),
    ).toEqual({ events: ["transaction.in_doubt"], from: [] });
  });

  it("keeps only the platform's event types", () => {
    expect(
      readDeclaration(
        workflow({
          eventTrigger: {
            events: ["transaction.opened", "transaction.completed", 7],
          },
        }),
      ),
    ).toEqual({ events: ["transaction.completed"], from: null });
  });

  it("starts a new trigger on the event that needs attention, from any workflow", () => {
    expect(DEFAULT_DECLARATION).toEqual({
      events: ["transaction.in_doubt"],
      from: null,
    });
  });
});

describe("writing the declaration", () => {
  it("writes the events once each, in the platform's order", () => {
    const written = withDeclaration(workflow(), {
      events: [
        "transaction.in_doubt",
        "transaction.completed",
        "transaction.in_doubt",
      ],
      from: null,
    });
    expect(written.spec["eventTrigger"]).toEqual({
      events: ["transaction.completed", "transaction.in_doubt"],
    });
  });

  it("writes the source workflows as from", () => {
    expect(withDeclaration(workflow(), ATTENTION).spec["eventTrigger"]).toEqual(
      {
        events: ["transaction.in_doubt"],
        from: ["move-between-systems"],
      },
    );
  });

  it("keeps an empty list of workflows rather than reacting to any", () => {
    expect(
      withDeclaration(workflow(), {
        events: ["transaction.in_doubt"],
        from: [],
      }).spec["eventTrigger"],
    ).toEqual({ events: ["transaction.in_doubt"], from: [] });
  });

  it("removes the declaration for null", () => {
    const written = withDeclaration(
      workflow({ eventTrigger: { events: ["transaction.in_doubt"] } }),
      null,
    );
    expect("eventTrigger" in written.spec).toBe(false);
  });

  it("keeps every other part of the workflow as it was, in its place", () => {
    const before = workflow({
      eventTrigger: { events: ["transaction.completed"] },
      connections: { ticketing: { connector: "weave-http" } },
    });
    const kept = JSON.stringify(before);
    const written = withDeclaration(before, ATTENTION);
    expect(JSON.stringify(before)).toBe(kept);
    expect(Object.keys(written.spec)).toEqual(Object.keys(before.spec));
    expect(
      JSON.stringify({
        ...written,
        spec: { ...written.spec, eventTrigger: 0 },
      }),
    ).toBe(
      JSON.stringify({ ...before, spec: { ...before.spec, eventTrigger: 0 } }),
    );
  });
});

describe("the tile's subtitle", () => {
  it.each<[string, EventDeclaration, string]>([
    [
      "one event from one workflow",
      ATTENTION,
      "Needs attention · from move-between-systems",
    ],
    [
      "three events from any workflow",
      {
        events: [
          "transaction.in_doubt",
          "transaction.compensated",
          "transaction.cancelled",
        ],
        from: null,
      },
      "3 events · any workflow",
    ],
    [
      "one event from two workflows",
      {
        events: ["transaction.compensated"],
        from: ["move-between-systems", "sync-references"],
      },
      "Undone · from 2 workflows",
    ],
    [
      "nothing chosen yet",
      { events: [], from: [] },
      "No events chosen · no workflow named",
    ],
  ])("says %s", (_name, declaration, subtitle) => {
    expect(eventSubtitle(declaration)).toBe(subtitle);
  });
});

describe("what is wrong with the declaration", () => {
  it("is nothing for events from named workflows", () => {
    expect(eventProblems(ATTENTION)).toEqual({});
  });

  it("asks for at least one event", () => {
    expect(eventProblems({ events: [], from: null })).toEqual({
      events: "Choose at least one event.",
    });
  });

  it("asks for a workflow when Only these workflows has none", () => {
    expect(
      eventProblems({ events: ["transaction.in_doubt"], from: [] }),
    ).toEqual({
      from: "Name at least one workflow, or choose Any workflow in this project.",
    });
  });

  it("allows at most 100 workflows", () => {
    const names = Array.from({ length: 101 }, (_, index) => `source-${index}`);
    expect(
      eventProblems({ events: ["transaction.in_doubt"], from: names }).from,
    ).toBe("Up to 100 workflows can be named here.");
    expect(
      eventProblems({ events: ["transaction.in_doubt"], from: names.slice(1) }),
    ).toEqual({});
  });

  it("names a workflow named twice", () => {
    expect(
      eventProblems({
        events: ["transaction.in_doubt"],
        from: [
          "move-between-systems",
          "sync-references",
          "move-between-systems",
        ],
      }).from,
    ).toBe("move-between-systems is named twice.");
  });

  it("names a name that can't be a workflow's", () => {
    expect(
      eventProblems({
        events: ["transaction.in_doubt"],
        from: ["move-between-systems", "move between"],
      }).from,
    ).toBe(
      "“move between” isn't a workflow name. Use letters, numbers, dots, underscores or hyphens, starting with a letter or number.",
    );
  });
});

describe("reacting to its own transactions", () => {
  it("warns when from names the workflow itself", () => {
    expect(
      loopWarning(
        {
          events: ["transaction.in_doubt"],
          from: ["move-between-systems", "notify-team"],
        },
        "notify-team",
      ),
    ).toBe(
      "notify-team reacts to its own transactions too. Reactions stop after 3 in a row.",
    );
  });

  it("is quiet for other workflows and for any workflow, which leaves its own out", () => {
    expect(loopWarning(ATTENTION, "notify-team")).toBeNull();
    expect(
      loopWarning(
        { events: ["transaction.in_doubt"], from: null },
        "notify-team",
      ),
    ).toBeNull();
  });
});

describe("what the tile says", () => {
  it.each<[string, EventDeclaration, string]>([
    ["one event and one workflow", ATTENTION, "Needs attention · 1 workflow"],
    [
      "one event from any workflow",
      { events: ["transaction.in_doubt"], from: null },
      "Needs attention · any workflow",
    ],
    [
      "several events",
      {
        events: [
          "transaction.in_doubt",
          "transaction.compensated",
          "transaction.completed",
        ],
        from: null,
      },
      "3 events · any workflow",
    ],
    [
      "several workflows",
      {
        events: ["transaction.in_doubt", "transaction.compensated"],
        from: ["move-between-systems", "sync-records"],
      },
      "2 events · 2 workflows",
    ],
    ["no event", { events: [], from: ["sync-records"] }, "No events chosen"],
    [
      "no workflow named yet",
      { events: ["transaction.in_doubt"], from: [] },
      "Needs attention · no workflow",
    ],
  ])("counts %s, so the subtitle fits", (_name, declaration, subtitle) => {
    expect(eventTileSubtitle(declaration)).toBe(subtitle);
  });

  it("names every event and workflow in full for the tooltip and the accessible name", () => {
    expect(
      eventTileName({
        events: ["transaction.in_doubt", "transaction.compensated"],
        from: ["move-between-systems", "sync-records"],
      }),
    ).toBe(
      "Platform event trigger: Needs attention and Undone, from move-between-systems and sync-records",
    );
    expect(
      eventTileName({ events: ["transaction.in_doubt"], from: null }),
    ).toBe(
      "Platform event trigger: Needs attention, from any workflow in this project",
    );
    expect(eventTileName({ events: [], from: [] })).toBe(
      "Platform event trigger: no events chosen, no workflow named",
    );
  });
});

describe("whether the workflow's input takes the platform event", () => {
  it.each<[string, unknown, "set" | "keep" | "ask"]>([
    ["no input", undefined, "set"],
    ["an empty input", {}, "set"],
    ["a new workflow's input", { type: "object" }, "set"],
    ["a form without fields", { type: "object", properties: {} }, "set"],
    [
      "an input closed to any field",
      { type: "object", additionalProperties: false },
      "ask",
    ],
    ["the platform event", { $ref: "weave.platform-event-v1" }, "keep"],
    [
      "a manual form's input",
      { type: "object", properties: { reference: { type: "string" } } },
      "ask",
    ],
    ["another named schema", { $ref: "customer.json" }, "ask"],
  ])("for %s", (_name, schema, fit) => {
    expect(eventInputFit(schema)).toBe(fit);
  });
});
