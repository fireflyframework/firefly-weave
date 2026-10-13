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
import { parse, stringify } from "yaml";
import {
  NOT_STEPS,
  copySteps,
  planPaste,
  readClipboard,
  stillUnreadable,
  unreadableMessage,
  type ClipboardContent,
  type PastePlan,
} from "../src/app/editor/state/clipboard";
import {
  ownedActionsOf,
  withOwnedAction,
} from "../src/app/editor/ndv/owned/owned-store";
import {
  newConnectorRecipe,
  newHttpRecipe,
} from "../src/app/editor/ndv/owned/owned-actions";
import { emptyCanvas } from "../src/app/editor/state/canvas-sidecar";
import type { DataSource, PinEntry } from "../src/app/editor/state/test-data";
import {
  StructuredCanvasAdapter,
  freshWorkflow,
  kinds,
  type Step,
} from "../src/app/model";

const source = `apiVersion: weave/v1alpha1
kind: Workflow
metadata: {name: order-intake, version: 1.0.0}
spec:
  inputSchema: {type: object}
  outputSchema: {type: object}
  connections:
    crm: {connector: weave-http@2.0.0, required: true}
  llmProfiles:
    default: {model: qwen3}
  steps:
    - {id: check-customer, kind: action, uses: onboarding.check-customer@1.0.0, with: {ref: /input}, connection: crm}
    - {id: summarize, kind: llm, uses: weave-agentic-generate@1.0.0, profile: default, connection: ai, prompt: {ref: /steps/check-customer/output}, context: {literal: {}}}
    - {id: notify, kind: transform, value: {ref: /steps/summarize/output}}
  output: {literal: {}}
`;
const model = () => {
  const adapter = new StructuredCanvasAdapter();
  adapter.setSource(source, "yaml");
  return adapter;
};
const zero = "fnv1a64:0000000000000000" as const;
const pin = (from: DataSource): PinEntry => ({
  output: {},
  source: from,
  pinnedAt: "2026-10-13T09:00:00Z",
  configHash: zero,
  contractHash: zero,
  upstreamHash: zero,
  ...(from === "run"
    ? {
        runId: "00000000-0000-4000-8000-000000000001",
        runEnvironmentId: "00000000-0000-4000-8000-000000000002",
      }
    : {}),
});
const read = (text: string): ClipboardContent => {
  const reading = readClipboard(text);
  if (!reading.ok) throw new Error(reading.reason);
  return reading.content;
};
const plan = (...args: Parameters<typeof planPaste>): PastePlan => {
  const planned = planPaste(...args);
  if ("reason" in planned) throw new Error(planned.reason);
  return planned;
};

describe("copying steps", () => {
  it("writes the run of steps as commented YAML with the slots, profiles and pins they use, never run data", () => {
    const copied = copySteps(
      model().definition,
      ["check-customer", "summarize"],
      {
        "check-customer": pin("manual"),
        "check-customer[2]": pin("simulated"),
        summarize: pin("run"),
        notify: pin("manual"),
      },
    );
    expect(
      copied.yaml.startsWith(
        "# Firefly Weave steps. Paste into Studio.\nweaveClipboard: 1\n",
      ),
    ).toBe(true);
    expect(parse(copied.yaml)).toEqual(JSON.parse(copied.json));
    expect(copied.document.steps.map((step) => step.id)).toEqual([
      "check-customer",
      "summarize",
    ]);
    expect(copied.document.connections).toEqual({
      crm: { connector: "weave-http@2.0.0", required: true },
    });
    expect(copied.document.llmProfiles).toEqual({
      default: { model: "qwen3" },
    });
    expect(Object.keys(copied.document.pins)).toEqual([
      "check-customer",
      "check-customer[2]",
    ]);
    expect(copied.document).not.toHaveProperty("actions");
  });
});

describe("reading the clipboard", () => {
  it("reads its own document as YAML or JSON, a list of steps, one step and a whole workflow", () => {
    const copied = copySteps(model().definition, ["notify"]);
    for (const text of [
      copied.yaml,
      copied.json,
      stringify(copied.document.steps),
      stringify(copied.document.steps[0]),
    ])
      expect(
        read(text).steps.map((step) => step.id),
        text,
      ).toEqual(["notify"]);
    const whole = read(source);
    expect(whole.steps.map((step) => step.id)).toEqual([
      "check-customer",
      "summarize",
      "notify",
    ]);
    expect(whole.connections).toEqual({
      crm: { connector: "weave-http@2.0.0", required: true },
    });
  });

  it("refuses what isn't steps, with one sentence", () => {
    const refused = (text: string) => readClipboard(text);
    expect(refused("https://example.com/orders")).toEqual({
      ok: false,
      reason: NOT_STEPS,
    });
    expect(refused("Approve the order before noon.")).toEqual({
      ok: false,
      reason: NOT_STEPS,
    });
    expect(refused("[1, 2, 3]")).toEqual({ ok: false, reason: NOT_STEPS });
    expect(refused("")).toEqual({ ok: false, reason: NOT_STEPS });
    expect(refused("a: [unclosed")).toEqual({ ok: false, reason: NOT_STEPS });
    expect(refused("- {kind: transform}")).toEqual({
      ok: false,
      reason: NOT_STEPS,
    });
    expect(refused("[]")).toEqual({
      ok: false,
      reason: "The clipboard holds no steps.",
    });
    expect(refused("- {id: a, kind: teleport}")).toEqual({
      ok: false,
      reason: "This paste has a step kind Studio doesn't know: teleport.",
    });
    expect(
      refused("- {id: a, kind: switch, cases: [{}], default: {steps: []}}"),
    ).toEqual({
      ok: false,
      reason: "The pasted step a has a path without a list of steps.",
    });
    expect(refused("base: &b {id: a, kind: wait}\nsteps: [*b]")).toEqual({
      ok: false,
      reason:
        "Studio doesn't paste YAML aliases (*name). Copy the steps without them.",
    });
    expect(
      refused(`- {id: a, kind: wait, note: "${"x".repeat(1_048_577)}"}`),
    ).toEqual({
      ok: false,
      reason: "The clipboard holds more than 1 MiB. Copy fewer steps.",
    });
  });
});

describe("planning a paste", () => {
  it("renumbers taken IDs and follows references between the pasted steps, leaving others as written", () => {
    const target = model();
    const planned = plan(
      target.definition,
      read(copySteps(target.definition, ["summarize", "notify"]).yaml),
      { owner: "root", index: 3 },
      target.stepIdSet(),
    );
    expect(planned.ids).toEqual(["summarize-1", "notify-1"]);
    expect(planned.renamed).toEqual({
      summarize: "summarize-1",
      notify: "notify-1",
    });
    expect(planned.steps[1]["value"]).toEqual({
      ref: "/steps/summarize-1/output",
    });
    expect(planned.steps[0]["prompt"]).toEqual({
      ref: "/steps/check-customer/output",
    });
    expect(planned.unreadable).toEqual([]);
    expect(planned.ownedAdds).toEqual([]);
  });

  it("finds references the new place can't read", () => {
    const target = model();
    const notify = read(copySteps(target.definition, ["notify"]).yaml);
    const early = plan(
      target.definition,
      notify,
      { owner: "root", index: 0 },
      target.stepIdSet(),
    );
    expect(early.unreadable).toEqual([
      {
        stepId: "notify-1",
        ref: "/steps/summarize/output",
        path: ["value"],
        fieldPath: "/value",
        target: "summarize",
        reason: "later",
      },
    ]);
    expect(unreadableMessage(early.unreadable)).toBe(
      "1 reference can't be read here (summarize runs later or on another path).",
    );
    const other = new StructuredCanvasAdapter();
    const elsewhere = plan(
      other.definition,
      notify,
      { owner: "root", index: 0 },
      other.stepIdSet(),
    );
    expect(elsewhere.unreadable[0]).toMatchObject({
      stepId: "notify",
      reason: "missing",
    });
    expect(unreadableMessage(elsewhere.unreadable)).toBe(
      "1 reference can't be read here (summarize isn't in this workflow).",
    );
  });

  it("shows an unreadable reference's step cut and without format controls", () => {
    const target = `\u202E${"z".repeat(5000)}`;
    const message = unreadableMessage([
      {
        stepId: "notify",
        ref: `/steps/${target}/output`,
        path: ["value"],
        fieldPath: "/value",
        target,
        reason: "missing",
      },
    ]);
    expect(message).toBe(
      `1 reference can't be read here (${"z".repeat(59)}… isn't in this workflow).`,
    );
    expect(message).not.toMatch(/\u202E/);
  });

  it("brings slots and AI profiles, renaming a slot whose name another connector holds", () => {
    const content = read(
      copySteps(model().definition, ["check-customer", "summarize"]).yaml,
    );
    const target = freshWorkflow();
    target.spec["connections"] = {
      crm: { connector: "weave-postgresql@1.0.0", required: true },
    };
    const planned = plan(
      target,
      content,
      { owner: "root", index: 0 },
      new Set(),
    );
    expect(planned.addConnections).toEqual({
      "crm-2": { connector: "weave-http@2.0.0", required: true },
    });
    expect(planned.renamedSlots).toEqual({ crm: "crm-2" });
    expect(planned.addProfiles).toEqual({ default: { model: "qwen3" } });
    expect(planned.steps[0]["connection"]).toBe("crm-2");
    const same = freshWorkflow();
    same.spec["connections"] = {
      crm: { connector: "weave-http@2.0.0", required: true },
    };
    expect(
      plan(same, content, { owner: "root", index: 0 }, new Set())
        .addConnections,
    ).toEqual({});
  });

  it("re-keys pins to renamed steps, instance keys included, and leaves run data and other steps' pins out", () => {
    const target = model();
    const content: ClipboardContent = {
      steps: [
        { id: "notify", kind: "transform", value: { literal: {} } } as Step,
      ],
      connections: {},
      llmProfiles: {},
      pins: {
        notify: pin("manual"),
        "notify[2]": pin("simulated"),
        "notify[3]": pin("run"),
        "notify-old": pin("manual"),
        other: pin("manual"),
      },
      actions: {},
    };
    const planned = plan(
      target.definition,
      content,
      { owner: "root", index: 3 },
      target.stepIdSet(),
    );
    expect(Object.keys(planned.pins).sort()).toEqual([
      "notify-1",
      "notify-1[2]",
    ]);
  });

  it("renumbers pasted steps that share an ID, nested ones included", () => {
    const content = read(
      stringify([
        { id: "a", kind: "wait", durationSeconds: 60 },
        {
          id: "a",
          kind: "switch",
          cases: [
            {
              steps: [{ id: "a", kind: "wait", durationSeconds: 60 }],
              output: { literal: {} },
            },
          ],
          default: { steps: [], output: { literal: {} } },
        },
      ]),
    );
    const planned = plan(
      freshWorkflow(),
      content,
      { owner: "root", index: 0 },
      new Set(),
    );
    expect(planned.ids).toEqual(["a", "a-1"]);
    expect(
      (planned.steps[1]["cases"] as { steps: Step[] }[])[0].steps[0].id,
    ).toBe("a-2");
    expect(planned.renamed).toEqual({});
  });

  it("refuses a place that is gone", () => {
    expect(
      planPaste(
        freshWorkflow(),
        read(source),
        { owner: "gone/case 1", index: 0 },
        new Set(),
      ),
    ).toEqual({
      reason:
        "That place in the workflow changed. Choose where to paste again.",
    });
  });

  it("knows when a pasted reference was fixed", () => {
    const target = model();
    const notify = read(copySteps(target.definition, ["notify"]).yaml);
    const early = plan(
      target.definition,
      notify,
      { owner: "root", index: 0 },
      target.stepIdSet(),
    );
    target.insertSteps("root", 0, early.steps);
    const pasted = target.definition.spec.steps[0];
    expect(
      stillUnreadable(target.definition, pasted, early.unreadable[0]),
    ).toBe(true);
    const fixed = { ...pasted, value: { literal: {} } };
    expect(stillUnreadable(target.definition, fixed, early.unreadable[0])).toBe(
      false,
    );
  });

  it("brings the actions pasted steps own, as the new workflow's own", () => {
    const sourceWorkflow = freshWorkflow();
    sourceWorkflow.metadata.name = "order-intake";
    sourceWorkflow.spec.steps = [
      {
        id: "call-api",
        kind: "action",
        uses: "order-intake.call-api@1.0.0",
        with: { literal: {} },
      },
      {
        id: "send-email",
        kind: "action",
        uses: "order-intake.send-email@1.0.0",
        with: { literal: {} },
      },
    ];
    let canvas = withOwnedAction(emptyCanvas(), "order-intake.call-api@1.0.0", {
      ...newHttpRecipe(),
      pathTemplate: "/orders",
    });
    canvas = withOwnedAction(
      canvas,
      "order-intake.send-email@1.0.0",
      newConnectorRecipe("weave-email@1.0.0", "send"),
    );
    const copied = copySteps(
      sourceWorkflow,
      ["call-api", "send-email"],
      {},
      ownedActionsOf(canvas),
    );
    expect(Object.keys(copied.document.actions ?? {})).toEqual([
      "order-intake.call-api@1.0.0",
      "order-intake.send-email@1.0.0",
    ]);
    const content = read(copied.yaml);
    // Into another workflow: each becomes that workflow's own, unpublished.
    const billing = freshWorkflow();
    billing.metadata.name = "billing";
    const into = plan(
      billing,
      content,
      { owner: "root", index: 0 },
      new Set(),
      {
        canvas: emptyCanvas(),
        reserved: new Set(),
      },
    );
    expect(into.steps.map((step) => step["uses"])).toEqual([
      "billing.call-api@1.0.0",
      "billing.send-email@1.0.0",
    ]);
    expect(into.ownedAdds).toEqual([
      {
        uses: "billing.call-api@1.0.0",
        recipe: { ...newHttpRecipe(), pathTemplate: "/orders" },
      },
      {
        uses: "billing.send-email@1.0.0",
        recipe: newConnectorRecipe("weave-email@1.0.0", "send"),
      },
    ]);
    // Into the same workflow: Call an API gets an action of its own; email shares the one it has.
    const again = plan(
      sourceWorkflow,
      content,
      { owner: "root", index: 2 },
      new Set(["call-api", "send-email"]),
      {
        canvas,
        reserved: new Set(),
      },
    );
    expect(again.steps.map((step) => step["uses"])).toEqual([
      "order-intake.call-api-1@1.0.0",
      "order-intake.send-email@1.0.0",
    ]);
    expect(again.ownedAdds.map((add) => add.uses)).toEqual([
      "order-intake.call-api-1@1.0.0",
    ]);
  });
});

describe("reading the clipboard safely", () => {
  it("refuses keys that would reach an object's prototype, and pollutes nothing", () => {
    for (const text of [
      '{"weaveClipboard": 1, "steps": [{"id": "a", "kind": "wait", "durationSeconds": 1}], "__proto__": {"polluted": true}}',
      '[{"id": "a", "kind": "transform", "value": {"literal": {"__proto__": {"polluted": true}}}}]',
      "- {id: a, kind: transform, value: {literal: {constructor: {prototype: {polluted: true}}}}}",
      "weaveClipboard: 1\nsteps: [{id: a, kind: wait, durationSeconds: 1}]\npins: {__proto__: {polluted: true}}",
    ])
      expect(readClipboard(text), text).toEqual({
        ok: false,
        reason: NOT_STEPS,
      });
    expect(({} as Record<string, unknown>)["polluted"]).toBeUndefined();
    expect(Object.prototype).not.toHaveProperty("polluted");
  });

  it("refuses fields it doesn't know, in the document, a step, a path or an action", () => {
    expect(
      readClipboard(
        "weaveClipboard: 1\nsteps: [{id: a, kind: wait, durationSeconds: 1}]\nscript: run",
      ),
    ).toEqual({
      ok: false,
      reason: "This paste has a field Studio doesn't know: script.",
    });
    expect(
      readClipboard("- {id: a, kind: wait, durationSeconds: 1, onLoad: run}"),
    ).toEqual({
      ok: false,
      reason: "The pasted step a has a field Studio doesn't know: onLoad.",
    });
    expect(
      readClipboard(
        "- {id: s, kind: switch, cases: [{when: {literal: true}, steps: [], output: {literal: {}}, extra: 1}], default: {steps: [], output: {literal: {}}}}",
      ),
    ).toEqual({
      ok: false,
      reason: "The pasted step s has a field Studio doesn't know: extra.",
    });
    expect(
      readClipboard(
        "weaveClipboard: 1\nsteps: [{id: a, kind: wait, durationSeconds: 1}]\nconnections: {crm: {connector: weave-http@2.0.0, token: t}}",
      ),
    ).toEqual({
      ok: false,
      reason: "This paste has a connection slot Studio can't read: crm.",
    });
    expect(
      readClipboard(
        "- {id: l, kind: forEach, items: {literal: []}, body: {steps: [{id: x, kind: wait, durationSeconds: 1, onLoad: run}], output: {literal: {}}}}",
        new Set([...kinds, "forEach"]),
      ),
    ).toEqual({
      ok: false,
      reason: "The pasted step x has a field Studio doesn't know: onLoad.",
    });
    const action = (recipe: string) =>
      readClipboard(
        `weaveClipboard: 1\nsteps: [{id: c, kind: action, uses: w.c@1.0.0, with: {literal: {}}}]\nactions: {w.c@1.0.0: ${recipe}}`,
      );
    expect(
      action(
        "{kind: http, method: GET, pathTemplate: /a, statuses: [200], timeoutSeconds: 30}",
      ).ok,
    ).toBe(true);
    for (const recipe of [
      "{kind: http, method: GET, pathTemplate: /a, statuses: [200], timeoutSeconds: 30, apiKey: k}",
      "{kind: http, method: TRACE, pathTemplate: /a, statuses: [200], timeoutSeconds: 30}",
      "{kind: http, method: GET, pathTemplate: /a, statuses: [two], timeoutSeconds: 30}",
      "{kind: connector, connector: weave-email@1.0.0, action: send, run: x}",
      "{kind: script, source: x}",
    ])
      expect(action(recipe), recipe).toEqual({
        ok: false,
        reason:
          "The pasted steps bring an action Studio can't read: w.c@1.0.0.",
      });
  });

  it("refuses values nested deeper than the canvas reads, without overflowing", () => {
    const deep = `- {id: a, kind: transform, value: {literal: ${"[".repeat(5000)}${"]".repeat(5000)}}}`;
    expect(readClipboard(deep)).toEqual({ ok: false, reason: NOT_STEPS });
  });
});

describe("what copying puts on the clipboard", () => {
  it("carries only what a recipe says, never other fields a canvas file was given", () => {
    const workflow = freshWorkflow();
    workflow.metadata.name = "order-intake";
    workflow.spec.steps = [
      {
        id: "call-api",
        kind: "action",
        uses: "order-intake.call-api@1.0.0",
        with: { literal: {} },
      },
    ];
    const canvas = withOwnedAction(
      emptyCanvas(),
      "order-intake.call-api@1.0.0",
      {
        ...newHttpRecipe(),
        pathTemplate: "/orders",
        apiKey: "s3cr3t-value",
      } as never,
    );
    const copied = copySteps(
      workflow,
      ["call-api"],
      {},
      ownedActionsOf(canvas),
    );
    expect(copied.yaml).not.toContain("s3cr3t-value");
    expect(copied.json).not.toContain("s3cr3t-value");
    expect(copied.document.actions).toEqual({
      "order-intake.call-api@1.0.0": {
        ...newHttpRecipe(),
        pathTemplate: "/orders",
      },
    });
    expect(Object.keys(copied.document).sort()).toEqual(
      [
        "actions",
        "connections",
        "llmProfiles",
        "pins",
        "steps",
        "weaveClipboard",
      ].sort(),
    );
  });
});

describe("pasting at the limits", () => {
  it("refuses more than 500 steps at a time, nested ones counted, with one sentence", () => {
    const wait = (i: number) => ({
      id: `w${i}`,
      kind: "wait",
      durationSeconds: 1,
    });
    expect(
      readClipboard(stringify(Array.from({ length: 500 }, (_, i) => wait(i))))
        .ok,
    ).toBe(true);
    expect(
      readClipboard(stringify(Array.from({ length: 501 }, (_, i) => wait(i)))),
    ).toEqual({
      ok: false,
      reason: "Studio pastes at most 500 steps at a time. Copy fewer steps.",
    });
    const nested = {
      id: "s",
      kind: "switch",
      cases: [
        {
          when: { literal: true },
          steps: Array.from({ length: 500 }, (_, i) => wait(i)),
          output: { literal: {} },
        },
      ],
      default: { steps: [], output: { literal: {} } },
    };
    expect(readClipboard(stringify([nested]))).toEqual({
      ok: false,
      reason: "Studio pastes at most 500 steps at a time. Copy fewer steps.",
    });
  });

  it("numbers 500 steps that share an ID in linear time", () => {
    const content = read(
      stringify(
        Array.from({ length: 500 }, () => ({
          id: "a",
          kind: "wait",
          durationSeconds: 1,
        })),
      ),
    );
    const taken = new Set(Array.from({ length: 5000 }, (_, i) => `a-${i + 1}`));
    const started = performance.now();
    const planned = plan(
      freshWorkflow(),
      content,
      { owner: "root", index: 0 },
      taken,
    );
    expect(performance.now() - started).toBeLessThan(150);
    expect(planned.ids.slice(0, 3)).toEqual(["a", "a-5001", "a-5002"]);
    expect(new Set(planned.ids).size).toBe(500);
  });

  it("gives a step ID longer than 128 characters a new one, as for any ID the language can't read", () => {
    const content = read(
      stringify([{ id: "a".repeat(129), kind: "wait", durationSeconds: 1 }]),
    );
    expect(
      plan(freshWorkflow(), content, { owner: "root", index: 0 }, new Set())
        .ids,
    ).toEqual(["wait-1"]);
    const fits = read(
      stringify([{ id: "a".repeat(128), kind: "wait", durationSeconds: 1 }]),
    );
    expect(
      plan(freshWorkflow(), fits, { owner: "root", index: 0 }, new Set()).ids,
    ).toEqual(["a".repeat(128)]);
  });

  it("never echoes more than 60 characters of what it refuses, nor text-direction controls", () => {
    const long = "k".repeat(10_000);
    for (const text of [
      `- {id: a, kind: ${long}}`,
      `- {id: a, kind: wait, durationSeconds: 1, ${long}: 1}`,
      `weaveClipboard: 1\nsteps: [{id: a, kind: wait, durationSeconds: 1}]\n${long}: 1`,
      `weaveClipboard: 1\nsteps: [{id: c, kind: action, uses: w.c@1.0.0, with: {literal: {}}}]\nactions: {w.c@1.0.0: {kind: script}, ${long}@1.0.0: {kind: script}}`,
    ]) {
      const reading = readClipboard(text);
      expect(reading.ok).toBe(false);
      const reason = reading.ok ? "" : reading.reason;
      expect(reason.length, reason.slice(0, 80)).toBeLessThan(160);
    }
    const reading = readClipboard('- {id: a, kind: "wa\u202Eit"}');
    expect(reading).toEqual({
      ok: false,
      reason: "This paste has a step kind Studio doesn't know: wait.",
    });
    const cut = readClipboard(`- {id: a, kind: ${"x".repeat(100)}}`);
    expect(cut).toEqual({
      ok: false,
      reason: `This paste has a step kind Studio doesn't know: ${"x".repeat(59)}….`,
    });
  });

  it("reads slot and AI profile names and slot connectors as the language does", () => {
    const paste = (connections: string, profiles = "{}") =>
      readClipboard(
        `weaveClipboard: 1\nsteps: [{id: a, kind: wait, durationSeconds: 1}]\nconnections: ${connections}\nllmProfiles: ${profiles}`,
      );
    expect(
      paste("{crm: {connector: weave-http@2.0.0}}", "{default: {model: qwen3}}")
        .ok,
    ).toBe(true);
    for (const [connections, name] of [
      ['{"c\u202Eevil": {connector: weave-http@2.0.0}}', "cevil"],
      ['{"-crm": {connector: weave-http@2.0.0}}', "-crm"],
      ["{crm: {connector: weave http}}", "crm"],
      ['{crm: {connector: "weave-http@2.0.0\u202E"}}', "crm"],
      [
        `{${"c".repeat(129)}: {connector: weave-http@2.0.0}}`,
        `${"c".repeat(59)}…`,
      ],
    ])
      expect(paste(connections), connections).toEqual({
        ok: false,
        reason: `This paste has a connection slot Studio can't read: ${name}.`,
      });
    for (const [profiles, name] of [
      ['{"de\u2066fault": {model: qwen3}}', "default"],
      ["{'a b': {model: qwen3}}", "a b"],
      ["{default: 7}", "default"],
    ])
      expect(paste("{}", profiles), profiles).toEqual({
        ok: false,
        reason: `This paste has an AI profile Studio can't read: ${name}.`,
      });
  });
});
