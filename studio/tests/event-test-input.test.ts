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
// The test input of a workflow whose input is the platform event: whichever
// Test event pane a person enters an event in, the IDs of a real event, run
// and incident never reach the shared test data.
import "@angular/compiler";
import {
  ChangeDetectorRef,
  ElementRef,
  Injector,
  runInInjectionContext,
  signal,
} from "@angular/core";
import {
  afterAll,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";

vi.hoisted(() =>
  vi.stubGlobal("document", {
    addEventListener: vi.fn(),
    // The formula editor reads this when step details load.
    documentElement: { style: {} },
  }),
);
import type { JsonObject } from "../src/app/forms/core/json";
import { StructuredCanvasAdapter } from "../src/app/model";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { TestEventPane } from "../src/app/editor/ndv/panes/test-event-pane";
import { FormSession } from "../src/app/editor/ndv/params/form-session";
import type { Json } from "../src/app/editor/ndv/registry";
import { StepDetailsController } from "../src/app/editor/ndv/step-details-controller";
import type { StepDetailsHost } from "../src/app/editor/ndv/step-details-host";
import { openRequest } from "../src/app/editor/ndv/step-details-service";
import { eventCount } from "../src/app/editor/triggers/event-sample";
import { EventSample } from "../src/app/editor/triggers/event-sample-pane";
import { TriggerEventPane } from "../src/app/editor/triggers/test-event-pane";
import { TaskForm } from "../src/app/task-form";

afterAll(() => vi.unstubAllGlobals());
beforeAll(() => loadKindRegistrations());

const REAL = {
  event: "a3d0c1b2-1111-4222-8333-000000000001",
  run: "a3d0c1b2-1111-4222-8333-000000000002",
  incident: "a3d0c1b2-1111-4222-8333-000000000003",
};
const SAMPLE_ID = /^00000000-0000-4000-8000-[0-9a-f]{12}$/;
interface Event {
  event_id: string;
  transaction: { run_id: string; incident_id?: string; business_key: string };
}
const realEvent = (facts: JsonObject = {}): JsonObject => ({
  version: "weave/platform-event-v1",
  event_id: REAL.event,
  type: "transaction.in_doubt",
  emitted_at: "2026-11-18T16:05:11Z",
  transaction: {
    workflow: { name: "sync-records", version: "2.0.1" },
    run_id: REAL.run,
    instance_key: "move",
    business_key: "REF-4410",
    state: "in_doubt",
    cause: { code: "WV-TXN-UNDO-STUCK", step: "add" },
    steps: { done: [], undone: [], pending: [] },
    incident_id: REAL.incident,
    test: false,
    ...facts,
  },
});

/** notify-team's step details on its input form, with test data in memory. */
function setup(inputSchema: unknown = { $ref: "weave.platform-event-v1" }) {
  const model = new StructuredCanvasAdapter();
  model.updateWorkflow({
    ...model.definition,
    spec: {
      ...model.definition.spec,
      inputSchema,
      eventTrigger: { events: ["transaction.in_doubt"] },
    },
  });
  model.clearHistory();
  const announced: string[] = [];
  const host = {
    model,
    editingLocked: false,
    profile: null,
    catalogContracts: new Map(),
    decisionContracts: new Map(),
    actionVersions: [],
    diagnostics: null,
    diagnosticsDefinition: null,
    label: (kind: string) => kind,
    can: () => true,
    notify: () => undefined,
    refreshView: () => undefined,
    perform: (edit: () => void) => edit(),
    // The Studio host has no platform event fields to serve here.
    api: { request: async () => Promise.reject(Error("busy")) },
  } as unknown as StepDetailsHost;
  const controller = new StepDetailsController(host);
  const session = new FormSession(
    host,
    controller,
    openRequest("$trigger"),
    { confirm: async () => true, announce: (text) => announced.push(text) },
    () => true,
  );
  const injector = Injector.create({
    providers: [
      {
        provide: ElementRef,
        useValue: new ElementRef({
          ownerDocument: { activeElement: null },
          querySelectorAll: () => [],
        }),
      },
      {
        provide: ChangeDetectorRef,
        useValue: { markForCheck() {}, detectChanges: () => draw.now() },
      },
    ],
  });
  /** What drawing the form does, for a test that looks at it. */
  const draw = { now: () => undefined as void };
  return { model, controller, session, injector, announced, draw };
}
const kept = (controller: StepDetailsController) =>
  controller.testEvent() as unknown as Event;

/** What the trigger's Output says over the test event's fields. */
const said = (controller: StepDetailsController) =>
  eventCount(controller.testEventSource(), controller.testEvent());

/** The manual form: its JSON box holds the whole event. */
function manualPane(f: ReturnType<typeof setup>) {
  const pane = runInInjectionContext(f.injector, () => new TestEventPane());
  pane.session = signal(f.session) as never;
  const owner = pane.forms()[0];
  const form = runInInjectionContext(f.injector, () => new TaskForm());
  form.schema = signal(owner.schema) as never;
  form.schemaRoot = signal(owner.root) as never;
  form.initialData = signal(owner.initial) as never;
  const writes: Promise<void>[] = [];
  form.dataChange.subscribe((data) =>
    writes.push(pane.changed(data, owner, form)),
  );
  form.ngOnChanges({ schema: {} as never });
  const target = (value: string) =>
    ({ target: { value, setCustomValidity() {} } }) as unknown as Event;
  return {
    pane,
    /** What the box shows, and the text a person leaves in it. */
    box: () => form.jsonText(["value"]),
    type: (text: string) => form.setJson(["value"], target(text), null),
    settled: () => Promise.all(writes),
    enter: async (value: unknown) => {
      form.setJson(["value"], target(JSON.stringify(value)), null);
      await Promise.all(writes);
    },
  };
}
/** A Kafka message trigger's pane, beside the platform event's. */
function kafkaPane(f: ReturnType<typeof setup>) {
  const pane = runInInjectionContext(f.injector, () => new TriggerEventPane());
  pane.session = signal(f.session) as never;
  pane.trigger = signal({ id: "trigger-0000000b", kind: "broker" }) as never;
  return {
    pane,
    enter: async (value: unknown) => {
      pane.paste();
      pane.draft.set(JSON.stringify(value));
      await pane.use();
    },
  };
}

describe.each([
  ["the manual form", manualPane],
  ["a Kafka message trigger", kafkaPane],
] as const)("a real platform event entered in %s", (_name, open) => {
  let f: ReturnType<typeof setup>;
  let opened: ReturnType<typeof manualPane> | ReturnType<typeof kafkaPane>;
  beforeEach(() => {
    f = setup();
    opened = open(f);
  });

  it("gets sample IDs in place of its event, run and incident IDs", async () => {
    await opened.enter(realEvent());
    const event = kept(f.controller);
    const ids = [
      event.event_id,
      event.transaction.run_id,
      event.transaction.incident_id,
    ];
    for (const id of ids) expect(id).toMatch(SAMPLE_ID);
    expect(new Set(ids).size).toBe(3);
    expect(event.transaction.business_key).toBe("REF-4410");
    expect(f.controller.replacedIds()).toEqual(["event", "run", "incident"]);
    for (const id of Object.values(REAL))
      expect(JSON.stringify(f.controller.testData.data())).not.toContain(id);
  });

  it("keeps the sample IDs it already has", async () => {
    const event = realEvent({
      run_id: "00000000-0000-4000-8000-00000000000b",
      incident_id: "00000000-0000-4000-8000-00000000000c",
    });
    event["event_id"] = "00000000-0000-4000-8000-00000000000a";
    await opened.enter(event);
    expect(kept(f.controller)).toEqual(event);
    expect(f.controller.replacedIds()).toEqual([]);
  });

  it("is refused with a field the platform event hasn't, which could keep a real ID", async () => {
    await opened.enter({ ...realEvent(), root_event_id: REAL.event });
    expect(f.controller.testEvent()).toBeUndefined();
    expect(opened.pane.problem()).toBe(
      "root_event_id isn't one of the platform event's fields.",
    );
  });
});

describe("the manual form's box", () => {
  it("shows the event as it's kept once its real IDs are replaced", async () => {
    const f = setup();
    const { box, enter } = manualPane(f);
    await enter(realEvent());
    for (const id of Object.values(REAL)) expect(box()).not.toContain(id);
    expect(box()).toBe(JSON.stringify(kept(f.controller), null, 2));
    // What comes next is an edit of that event.
    const next = JSON.parse(box()) as Event;
    next.transaction.business_key = "REF-5";
    await enter(next);
    expect(kept(f.controller)).toEqual(next);
  });

  it("keeps the text a person types when nothing in it is replaced", async () => {
    const f = setup();
    const { box, enter } = manualPane(f);
    await enter(realEvent());
    const compact = JSON.stringify(JSON.parse(box()));
    await enter(JSON.parse(compact));
    expect(box()).toBe(compact);
  });

  it("shows the kept event again when the same real one is typed over it before it's drawn", async () => {
    const f = setup();
    const { box, enter, type, settled } = manualPane(f);
    await enter(realEvent());
    // As Angular draws a box: only a text that isn't the last one it drew.
    let last = box();
    let onScreen = last;
    f.draw.now = () => {
      if (box() !== last) onScreen = last = box();
    };
    const text = JSON.stringify(realEvent());
    onScreen = text;
    type(text);
    await settled();
    expect(onScreen).toBe(JSON.stringify(kept(f.controller), null, 2));
  });

  it("keeps text that isn't JSON yet, and shows the kept event once it is", async () => {
    const f = setup();
    const { box, type, settled } = manualPane(f);
    const text = JSON.stringify(realEvent());
    type(text);
    type(text.slice(0, -1));
    await settled();
    expect(kept(f.controller).event_id).toMatch(SAMPLE_ID);
    expect(box()).toBe(text.slice(0, -1));
    type(text);
    await settled();
    expect(box()).toBe(JSON.stringify(kept(f.controller), null, 2));
  });

  it("shows the last of two changes, never the first one's event over it", async () => {
    const f = setup();
    const { box, type, settled } = manualPane(f);
    type(JSON.stringify(realEvent()));
    type(JSON.stringify(realEvent({ business_key: "REF-2" })));
    await settled();
    expect(kept(f.controller).transaction.business_key).toBe("REF-2");
    expect(box()).toBe(JSON.stringify(kept(f.controller), null, 2));
  });

  it("keeps a refused change that came while an event was being kept, and its problem", async () => {
    const f = setup();
    const { pane, box, type, settled } = manualPane(f);
    type(JSON.stringify(realEvent()));
    await Promise.resolve();
    const refused = JSON.stringify({ ...realEvent(), root_event_id: "x" });
    type(refused);
    await settled();
    expect(kept(f.controller).event_id).toMatch(SAMPLE_ID);
    expect(box()).toBe(refused);
    expect(pane.problem()).toBe(
      "root_event_id isn't one of the platform event's fields.",
    );
  });

  it("shows the kept events of a workflow's own input in its fields", async () => {
    const f = setup({
      type: "object",
      properties: {
        event: { type: "object" },
        note: { type: "string" },
      },
    });
    const pane = runInInjectionContext(f.injector, () => new TestEventPane());
    pane.session = signal(f.session) as never;
    const owner = pane.forms()[0]!;
    const shown: unknown[] = [];
    await pane.changed({ event: realEvent(), note: "x" }, owner, {
      showData: (data: unknown) => shown.push(data),
    } as unknown as TaskForm);
    expect(shown).toEqual([f.controller.testEvent()]);
    for (const id of Object.values(REAL))
      expect(JSON.stringify(shown)).not.toContain(id);
  });
});

it("says nothing of the IDs it replaced once the test event is another", async () => {
  const f = setup();
  await f.controller.setTestEvent(realEvent() as Json);
  expect(f.controller.replacedIds()).toEqual(["event", "run", "incident"]);
  // As another window, Debug in editor or Undo leaves it.
  const data = f.controller.testData.data();
  await f.controller.testData.change(
    { ...data, input: { value: { type: "x" }, source: "manual" } },
    [],
  );
  expect(f.controller.replacedIds()).toEqual([]);
});

describe("Generate sample in the manual form", () => {
  it("builds an event the trigger reacts to, the first in the choices", async () => {
    const f = setup();
    const { pane } = manualPane(f);
    f.model.updateWorkflow({
      ...f.model.definition,
      spec: {
        ...f.model.definition.spec,
        eventTrigger: {
          events: ["transaction.completed", "transaction.compensated"],
          from: ["sync-records"],
        },
      },
    });
    await pane.generate();
    expect(pane.problem()).toBe("");
    expect(kept(f.controller)).toMatchObject({
      type: "transaction.compensated",
      transaction: { workflow: { name: "sync-records" }, state: "compensated" },
    });
    expect(kept(f.controller).event_id).toMatch(SAMPLE_ID);
    expect(f.controller.testEventSource()).toBe("schema");
    // The form shows it.
    expect(pane.forms()[0]!.initial).toEqual({ value: kept(f.controller) });
    expect(f.announced).toEqual(["Generated a sample test event."]);
  });
});

describe("where the test event came from", () => {
  const from = async (entry: Record<string, unknown>) => {
    const f = setup();
    await f.controller.testData.change(
      {
        ...f.controller.testData.data(),
        input: { value: realEvent() as Json, ...entry } as never,
      },
      [],
    );
    return f.controller;
  };
  const RUN = { runId: REAL.run, runEnvironmentId: REAL.incident };

  it("is the run, for Debug in editor: the event isn't a pasted one", async () => {
    const controller = await from({ source: "run", ...RUN });
    expect(said(controller)).toBe("From a run · 5 fields");
    const pane = Object.assign(Object.create(EventSample.prototype), {
      session: () => ({ controller }),
    }) as EventSample;
    expect(pane.pasted()).toBe(false);
    expect(pane.ready()).toBe("From a run.");
  });

  it("is the test call that received it", async () => {
    const controller = await from({ source: "test-call", ...RUN });
    expect(said(controller)).toBe("From a test call · 5 fields");
  });

  it("is the person who pasted it, or the sample Studio built", async () => {
    expect(said(await from({ source: "manual" }))).toBe("Pasted · 5 fields");
    expect(said(await from({ source: "schema" }))).toBe("Sample · 5 fields");
  });
});

describe("a real ID entered again", () => {
  it.each([
    ["the manual form", manualPane],
    ["a Kafka message trigger", kafkaPane],
  ] as const)(
    "gets the sample ID it had in %s, so an edit that is undone leaves the event as it was",
    async (_name, open) => {
      const f = setup();
      const { enter } = open(f);
      await enter(realEvent());
      const first = kept(f.controller);
      await enter(realEvent({ business_key: "REF-5" }));
      expect(kept(f.controller)).toEqual({
        ...first,
        transaction: { ...first.transaction, business_key: "REF-5" },
      });
      await enter(realEvent());
      expect(kept(f.controller)).toEqual(first);
      expect(f.controller.replacedIds()).toEqual(["event", "run", "incident"]);
    },
  );

  it.each([
    [
      "the workflow opens again",
      (f: ReturnType<typeof setup>) =>
        f.controller.testData.open({ kind: "memory" }),
    ],
    [
      "the account signs out",
      (f: ReturnType<typeof setup>) => f.controller.testData.forget(),
    ],
  ] as const)("gets another sample ID once %s", async (_when, leave) => {
    const f = setup();
    const { enter } = kafkaPane(f);
    await enter(realEvent());
    const first = kept(f.controller).event_id;
    await leave(f);
    await enter(realEvent());
    expect(kept(f.controller).event_id).toMatch(SAMPLE_ID);
    expect(kept(f.controller).event_id).not.toBe(first);
  });
});

describe("what isn't a platform event", () => {
  const FORM = {
    type: "object",
    properties: { event_id: { type: "string" } },
  };

  it("keeps its IDs in a workflow with an input of its own", async () => {
    const f = setup(FORM);
    const value = { event_id: REAL.event };
    expect(await f.controller.setTestEvent(value)).toBeNull();
    expect(f.controller.testEvent()).toEqual(value);
    expect(f.controller.replacedIds()).toEqual([]);
  });

  it("is a platform event all the same when it says so: its IDs go in any workflow", async () => {
    const f = setup({ type: "object" });
    expect(await f.controller.setTestEvent(realEvent() as Json)).toBeNull();
    expect(kept(f.controller).event_id).toMatch(SAMPLE_ID);
    expect(kept(f.controller).transaction.run_id).toMatch(SAMPLE_ID);
  });
});

describe("a real platform event inside what a person enters", () => {
  const { version: _version, ...unversioned } = realEvent();
  const INSIDE: [where: string, value: Json, type?: string][] = [
    ["in a field", { event: realEvent() }],
    ["two levels deep", { body: { event: realEvent() } }],
    ["in a list", [realEvent()], "array"],
    ["without its version", unversioned],
  ];

  it.each(INSIDE)(
    "%s keeps no real ID in a workflow with an input of its own, and the pane says so",
    async (_where, value, type = "object") => {
      const f = setup({ type });
      const { pane, enter } = kafkaPane(f);
      await enter(value);
      const kept = JSON.stringify(f.controller.testData.data().input);
      expect(kept).toContain("REF-4410");
      for (const id of Object.values(REAL)) expect(kept).not.toContain(id);
      expect(pane.replaced()).toBe(
        "Studio replaced the event, run and incident IDs with sample IDs. Test data is shared with everyone who can test this workflow. Use a made-up business key.",
      );
    },
  );

  it("keeps none under a schema that marks its version secret", async () => {
    const f = setup({
      type: "object",
      properties: { version: { type: "string", "x-secret": true } },
    });
    await kafkaPane(f).enter(realEvent());
    const kept = JSON.stringify(f.controller.testData.data().input);
    expect(kept).toContain("REF-4410");
    for (const id of Object.values(REAL)) expect(kept).not.toContain(id);
  });

  it.each(INSIDE)(
    "%s is refused where the input is the platform event",
    async (_where, value) => {
      const f = setup();
      const { pane, enter } = kafkaPane(f);
      await enter(value);
      expect(f.controller.testEvent()).toBeUndefined();
      expect(pane.problem()).not.toBe("");
    },
  );
});
