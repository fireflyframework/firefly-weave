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
import { beforeAll, describe, expect, it } from "vitest";
import { loadKindRegistrations } from "../src/app/editor/ndv/kinds";
import { TEXT_RESULT } from "../src/app/editor/ndv/kinds/ai-task-model";
import { formEnv, stepSubject } from "../src/app/editor/ndv/params/form-env";
import { formState } from "../src/app/editor/ndv/params/form-model";
import { designerSchema } from "../src/app/editor/ndv/params/schema-params";
import { ndvRegistry, type KindContext } from "../src/app/editor/ndv/registry";
import {
  createStep,
  freshWorkflow,
  type Step,
  type Workflow,
} from "../src/app/model";

beforeAll(() => loadKindRegistrations());

const context = (workflow: Workflow): KindContext => ({
  workflow,
  features: [],
  actionContract: () => null,
  tableContract: () => null,
  workflowContract: () => null,
});
const settings = {
  provider: "openai-chat",
  model: "qwen3:4b",
  options: { max_tokens: 1024 },
  outputSchema: TEXT_RESULT,
};
function task(
  chosen: boolean,
  profiles: Record<string, unknown> = {},
): { workflow: Workflow; step: Step } {
  const workflow = freshWorkflow();
  const step = {
    ...createStep("llm", "summarize"),
    ...(chosen ? { profile: "summarize" } : {}),
  };
  workflow.spec.steps.push(step);
  workflow.spec["llmProfiles"] = {
    ...(chosen ? { summarize: settings } : {}),
    ...profiles,
  };
  return { workflow, step };
}
function state(
  form: "form" | "settings",
  { workflow, step }: { workflow: Workflow; step: Step },
) {
  const descriptor = ndvRegistry.kind("llm")!;
  const ctx = context(workflow);
  return formState(
    descriptor[form]!(step, ctx),
    formEnv(stepSubject(descriptor, step, ctx, null), step, ctx),
  );
}

describe("the AI task's parameters", () => {
  it("shows exactly Model and Prompt on a new AI task, both required", () => {
    const { fields } = state("form", task(false));
    expect(
      fields.map((f) => [f.spec.label, f.spec.type, !!f.spec.required]),
    ).toEqual([
      ["Model", "model", true],
      ["Prompt", "multiline", true],
    ]);
    expect(fields[0].spec).toMatchObject({
      scope: "workflow",
      path: ["spec", "llmProfiles", "default", "model"],
      hint: "Models are grouped by AI connection. The connection becomes the slot the step uses.",
    });
    expect(fields[1].spec).toMatchObject({
      path: ["prompt"],
      mapping: "both",
      templateCapable: true,
      placeholder: "Summarize {{ the ticket text }} in two sentences.",
    });
  });
  it("waits for a model before offering the options that tune it", () => {
    expect(
      state("form", task(false)).addable.map((o) => [o.spec.label, o.disabled]),
    ).toEqual([
      ["Context", null],
      ["Structured result", "Choose a model first."],
      ["Max output tokens", "Choose a model first."],
      ["Temperature", "Choose a model first."],
      ["Reasoning pattern", "Choose a model first."],
      ["Share settings with other AI steps", null],
    ]);
  });
  it("tunes the step's own settings once a model is chosen", () => {
    const { addable } = state("form", task(true));
    expect(addable.every((o) => o.disabled === null)).toBe(true);
    const paths = Object.fromEntries(
      addable.map((o) => [o.spec.id, [o.spec.scope ?? "step", ...o.spec.path]]),
    );
    expect(paths).toEqual({
      context: ["step", "context"],
      result: ["workflow", "spec", "llmProfiles", "summarize", "outputSchema"],
      maxTokens: [
        "workflow",
        "spec",
        "llmProfiles",
        "summarize",
        "options",
        "max_tokens",
      ],
      temperature: [
        "workflow",
        "spec",
        "llmProfiles",
        "summarize",
        "options",
        "temperature",
      ],
      reasoning: [
        "workflow",
        "spec",
        "llmProfiles",
        "summarize",
        "reasoning",
        "pattern",
      ],
      share: ["step", "profile"],
    });
    const byId = Object.fromEntries(addable.map((o) => [o.spec.id, o.spec]));
    expect(byId["result"]).toMatchObject({
      type: "schema",
      default: TEXT_RESULT,
      whenRemoved: "default",
    });
    expect(byId["maxTokens"]).toMatchObject({
      default: 1024,
      min: 1,
      max: 32768,
      whenRemoved: "default",
    });
    expect(byId["temperature"]).toMatchObject({
      min: 0,
      max: 2,
      placeholder: "Model default",
    });
    expect(byId["temperature"].default).toBeUndefined();
    expect(byId["reasoning"]).toMatchObject({ default: "none" });
  });
  it("reads settings only this step uses as its own, whatever their name", () => {
    // A renamed step keeps the settings named after its old ID.
    const renamed = task(true);
    renamed.step.id = "triage";
    const own = state("form", renamed);
    const share = own.addable.find((o) => o.spec.id === "share")!.spec;
    expect(share.default).toBe("summarize");
    expect(share.choices![0]).toEqual({
      value: "summarize",
      label: "This step only",
    });
    expect(own.fields.map((f) => f.spec.id)).toEqual(["model", "prompt"]);
    // Settings named "<step>-2" because "<step>" was taken are this step's too.
    const taken = task(false, {
      summarize: { ...settings, model: "qwen2.5:1.5b" },
      "summarize-2": settings,
    });
    taken.step["profile"] = "summarize-2";
    taken.workflow.spec.steps.push({
      ...createStep("llm", "triage"),
      profile: "summarize",
    });
    const second = state("form", taken);
    expect(second.fields.map((f) => f.spec.id)).toEqual(["model", "prompt"]);
    expect(
      second.addable.find((o) => o.spec.id === "share")!.spec.choices,
    ).toEqual([
      { value: "summarize-2", label: "This step only" },
      { value: "summarize", label: "summarize · qwen2.5:1.5b" },
    ]);
  });
  it("offers settings of its own when it shares another step's", () => {
    const sharing = task(false, { triage: settings });
    sharing.step["profile"] = "triage";
    sharing.workflow.spec.steps.push({
      ...createStep("llm", "triage"),
      profile: "triage",
    });
    const share = state("form", sharing).fields.find(
      (f) => f.spec.id === "share",
    )!.spec;
    expect(share.default).toBe("summarize");
    expect(share.choices![0]).toEqual({
      value: "summarize",
      label: "This step only",
    });
  });
  it("offers this step's own settings or another AI step's to share", () => {
    const shared = task(true, {
      triage: { ...settings, model: "qwen2.5:1.5b" },
    });
    const share = state("form", shared).addable.find(
      (o) => o.spec.id === "share",
    )!.spec;
    expect(share).toMatchObject({
      type: "select",
      default: "summarize",
      whenRemoved: "default",
    });
    expect(share.choices).toEqual([
      { value: "summarize", label: "This step only" },
      { value: "triage", label: "triage · qwen2.5:1.5b" },
    ]);
  });
});

describe("the AI task's settings and canvas", () => {
  it("keeps the timeout on Settings, with the step's settings", () => {
    const before = state("settings", task(false)).fields;
    expect(before.map((f) => [f.spec.label, f.readOnly])).toEqual([
      ["Timeout", "Choose a model first."],
    ]);
    const [timeout] = state("settings", task(true)).fields;
    expect(timeout.readOnly).toBeNull();
    expect(timeout.spec).toMatchObject({
      label: "Timeout",
      type: "duration",
      scope: "workflow",
      path: ["spec", "llmProfiles", "summarize", "timeoutSeconds"],
      default: 120,
      max: 600,
    });
  });
  it("names its model on the canvas, asks for one before, and replays AI turns", () => {
    const descriptor = ndvRegistry.kind("llm")!;
    const fresh = task(false);
    expect(descriptor.summary(fresh.step, context(fresh.workflow))).toBe(
      "Choose a model",
    );
    const chosen = task(true);
    expect(descriptor.summary(chosen.step, context(chosen.workflow))).toBe(
      "qwen3:4b",
    );
    expect(descriptor.script).toBe("ai-turns");
    expect(descriptor.pinnable).toBe(true);
  });
});

describe("a structured result", () => {
  it("starts with no fields while the result is still text", () => {
    expect(designerSchema(TEXT_RESULT, TEXT_RESULT)).toBeUndefined();
    expect(designerSchema(undefined, TEXT_RESULT)).toBeUndefined();
    const fields = {
      type: "object",
      properties: { summary: { type: "string" } },
    };
    expect(designerSchema(fields, TEXT_RESULT)).toEqual(fields);
    // Other schema fields keep what they hold, defaults included.
    expect(designerSchema(undefined, { type: "object" })).toEqual({
      type: "object",
    });
    expect(designerSchema({ type: "string" }, { type: "object" })).toEqual({
      type: "string",
    });
  });
});
