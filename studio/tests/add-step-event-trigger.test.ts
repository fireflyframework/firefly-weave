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
import "@angular/compiler";
import { describe, expect, it } from "vitest";
import { inputFitQuestion } from "../src/app/editor/add-step/add-trigger";
import { buildCatalog } from "../src/app/editor/add-step/entries";
import {
  GONE,
  insertEntry,
  placeMark,
  type InsertHost,
} from "../src/app/editor/add-step/insert";
import { matchScore } from "../src/app/editor/add-step/search";
import { StructuredCanvasAdapter } from "../src/app/model";
import type { DialogOptions } from "../src/app/dialog";
import { catalogInput, entryById, viewLabels } from "./add-step-fixtures";

const REFERENCE = { $ref: "weave.platform-event-v1" };
const FORM = {
  type: "object",
  required: ["reference"],
  properties: { reference: { type: "string" }, message: { type: "string" } },
};
const reacting = (extra: Parameters<typeof catalogInput>[0] = {}) =>
  buildCatalog(
    catalogInput({
      language: { features: ["flow.eventTrigger"], pending: new Set() },
      hasSteps: false,
      ...extra,
    }),
  );
const platformEvent = (catalog: ReturnType<typeof buildCatalog>) =>
  entryById(catalog, "trigger:event");

describe("Platform event in Add a step", () => {
  it("comes last of the triggers, with what it does", () => {
    const catalog = reacting();
    expect(viewLabels(catalog, "triggers").slice(6, 8)).toEqual([
      "Called by a workflow",
      "Platform event",
    ]);
    expect(platformEvent(catalog)).toMatchObject({
      description: "Start a run when a transaction needs attention",
      icon: "signal",
      shape: "trigger",
      disabled: null,
    });
  });

  it.each(["transaction", "doubt", "undone", "force ended", "react"])(
    "is found by %s",
    (word) => {
      expect(matchScore(word, platformEvent(reacting()))).not.toBeNull();
    },
  );

  it("is disabled, with the reason, on a platform without platform event triggers", () => {
    expect(
      platformEvent(
        reacting({
          language: { features: ["text.concat"], pending: new Set() },
        }),
      ).disabled,
    ).toBe("Needs a platform with platform event triggers (flow.eventTrigger)");
    expect(platformEvent(reacting({ language: null })).disabled).toBe(
      "Needs a platform with platform event triggers (flow.eventTrigger)",
    );
  });

  it("is disabled while the target's Studio can't edit it", () => {
    expect(
      platformEvent(
        reacting({
          language: {
            features: ["flow.eventTrigger"],
            pending: new Set(["eventTrigger"]),
          },
        }),
      ).disabled,
    ).toBe("Studio can't edit platform event triggers yet.");
  });

  it("is disabled once the workflow has one, and at the trigger limit", () => {
    expect(
      platformEvent(
        reacting({ triggers: [{ id: "trigger-0000000a", kind: "event" }] }),
      ).disabled,
    ).toBe("This workflow already starts from platform events.");
    expect(
      platformEvent(
        reacting({
          triggers: Array.from({ length: 6 }, (_, i) => ({
            id: `trigger-0000000${i}`,
            kind: "webhook" as const,
          })),
        }),
      ).disabled,
    ).toBe("A workflow can have at most 6 triggers.");
  });
});

/** A shell that answers the input question as given, and what it was asked and told. */
function shell(
  inputSchema: unknown,
  answer: boolean | ((host: InsertHost) => boolean) = false,
) {
  const model = new StructuredCanvasAdapter();
  const workflow = structuredClone(model.definition);
  workflow.spec["inputSchema"] = inputSchema;
  model.updateWorkflow(workflow);
  const asked: DialogOptions[] = [];
  const calls: string[] = [];
  const host = {
    model,
    editingLocked: false,
    ensureApplied: async () => true,
    edit(change: () => void) {
      try {
        change();
        return "";
      } catch (error) {
        return error instanceof Error ? error.message : String(error);
      }
    },
    confirm: async (question: DialogOptions) => {
      asked.push(question);
      return typeof answer === "function" ? answer(host) : answer;
    },
    notify: (text: string) => void calls.push(`notify ${text}`),
    openTrigger: (id: string, kind: string) =>
      void calls.push(`trigger ${kind} ${id}`),
  } as unknown as InsertHost;
  const choose = () =>
    insertEntry(host, platformEvent(reacting()), {
      at: { owner: "root", index: 0 },
      opened: model.opened,
      mark: placeMark(model, { owner: "root", index: 0 }),
    });
  return { model, asked, calls, choose };
}

describe("choosing Platform event", () => {
  it("declares a transaction that needs attention and takes the platform event as the input, as one undo step", async () => {
    const { model, asked, calls, choose } = shell({ type: "object" });
    const before = model.source;
    expect(await choose()).toBe("done");
    expect(asked).toEqual([]);
    expect(model.definition.spec["eventTrigger"]).toEqual({
      events: ["transaction.in_doubt"],
    });
    expect(model.definition.spec["inputSchema"]).toEqual(REFERENCE);
    const [intent] = model.canvas.triggers ?? [];
    expect(intent).toMatchObject({ kind: "event" });
    expect(calls).toEqual([`trigger event ${intent.id}`]);
    model.undo();
    expect(model.source).toBe(before);
    expect(model.canvas.triggers).toBeUndefined();
  });

  it("asks nothing when the input already is the platform event", async () => {
    const { model, asked, choose } = shell(REFERENCE);
    expect(await choose()).toBe("done");
    expect(asked).toEqual([]);
    expect(model.definition.spec["inputSchema"]).toEqual(REFERENCE);
  });

  it("replaces another input when the person says so, with the trigger in one undo step", async () => {
    const { model, asked, choose } = shell(FORM, true);
    expect(await choose()).toBe("done");
    expect(asked).toHaveLength(1);
    expect(model.definition.spec["inputSchema"]).toEqual(REFERENCE);
    expect(model.definition.spec["eventTrigger"]).toBeDefined();
    model.undo();
    expect(model.definition.spec["inputSchema"]).toEqual(FORM);
    expect("eventTrigger" in model.definition.spec).toBe(false);
    expect(model.canvas.triggers).toBeUndefined();
  });

  it("adds the trigger and keeps the input when the person keeps it", async () => {
    const { model, choose } = shell(FORM, false);
    expect(await choose()).toBe("done");
    expect(model.definition.spec["inputSchema"]).toEqual(FORM);
    expect(model.definition.spec["eventTrigger"]).toEqual({
      events: ["transaction.in_doubt"],
    });
  });

  it("adds nothing when the workflow changed while the question was open", async () => {
    const { model, calls, choose } = shell(FORM, (host) => {
      host.model.setSource(host.model.source.replace("untitled", "renamed"));
      return true;
    });
    expect(await choose()).toBe("gone");
    expect("eventTrigger" in model.definition.spec).toBe(false);
    expect(model.definition.spec["inputSchema"]).toEqual(FORM);
    expect(calls).toEqual([`notify ${GONE}`]);
  });
});

describe("the question before another input is replaced", () => {
  it("names the fields that would go and starts on keeping them", () => {
    expect(inputFitQuestion(FORM)).toEqual({
      title: "Replace this workflow's input?",
      message:
        "Runs that Platform event starts get the platform event as their input. This workflow's input doesn't fit it: it has 2 fields.\n\nReplace the input to read the event's fields in later steps. Keep the input to set it up yourself: the trigger can't start runs until the input accepts the platform event.",
      items: [
        { icon: "typeText", text: "reference" },
        { icon: "typeText", text: "message" },
      ],
      itemStyle: "chips",
      cancelLabel: "Keep the input",
      confirmLabel: "Replace the input",
      startOnCancel: true,
    });
  });

  it("lists six fields and counts the rest", () => {
    const properties = Object.fromEntries(
      Array.from({ length: 10 }, (_, i) => [`f${i}`, { type: "number" }]),
    );
    const question = inputFitQuestion({ type: "object", properties });
    expect(question.items?.map((item) => item.text)).toEqual([
      "f0",
      "f1",
      "f2",
      "f3",
      "f4",
      "f5",
      "and 4 more",
    ]);
    expect(question.message).toContain("it has 10 fields.");
  });

  it("says one field, and an input without fields, in words", () => {
    expect(
      inputFitQuestion({
        type: "object",
        properties: { reference: { type: "string" } },
      }).message,
    ).toContain("it has 1 field.");
    const named = inputFitQuestion({ $ref: "customer.json" });
    expect(named.message).toContain("This workflow's input doesn't fit it.\n");
    expect(named.items).toBeUndefined();
  });
});
