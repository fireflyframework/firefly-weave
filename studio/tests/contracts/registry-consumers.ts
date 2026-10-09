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
// Compile-only: registrations written the way the AI step components and loop
// and call support write them. `npm run check` type-checks this file; nothing
// runs it.
import { Component, input } from "@angular/core";
import {
  NDV_REGISTRY_VERSION,
  registerParameters,
  registerSubNodes,
  type Choice,
  type Edit,
  type InstanceKey,
  type Json,
  type KindContext,
  type NdvContext,
  type ParamSpec,
  type ParameterComponent,
  type RealExecutionSupport,
  type SampleContext,
  type StepKindDescriptor,
  type SubNodeSlotSpec,
} from "../../src/app/editor/ndv/registry";
import type { StepTestRequest } from "../../src/app/editor/state/execution";

// A registry version 3 fails here first.
export const pinnedVersion: NdvContext["version"] = NDV_REGISTRY_VERSION;

@Component({ selector: "contract-agent-parameters", template: "" })
export class AgentParameters implements ParameterComponent {
  readonly context = input.required<NdvContext>();

  chooseModel(model: string): void {
    const changes: Edit[] = [
      {
        path: ["spec", "llmProfiles", "support"],
        value: { provider: "ollama", model },
        scope: "workflow",
      },
      { path: ["profile"], value: "support" },
    ];
    this.context().edit(changes, "Choose the model");
  }
  addTool(): void {
    this.context().openAddStep({ slot: "tools" });
  }
  openTool(index: number): void {
    this.context().openSubNode("tools", index);
  }
  turns(): Json | null {
    return this.context().sample.script();
  }
  callable(): Promise<{ uses: string; title: string; disabled?: string }[]> {
    return this.context().catalog.workflows({ callableBy: "support-flow" });
  }
}

const tools = (step: { [key: string]: unknown }) =>
  Array.isArray(step["tools"]) ? (step["tools"] as { name: string }[]) : [];
export const agentSlots: SubNodeSlotSpec[] = [
  {
    id: "model",
    label: "Model",
    required: true,
    max: 1,
    chip: (step) => ({
      text: String(step["model"] ?? "Model"),
      state: step["model"] ? "ok" : "missing",
    }),
    entries: async () => [],
  },
  {
    id: "tools",
    label: "Tools",
    required: false,
    max: null,
    chip: (step) => ({ text: `Tools (${tools(step).length})`, state: "ok" }),
    items: (step) =>
      tools(step).map((tool) => ({ text: tool.name, state: "ok" })),
    entries: async (ctx) => [
      {
        id: "orders-get",
        label: "orders-get",
        icon: "action",
        group: "Actions",
        apply: () =>
          ctx.edit(
            [{ path: ["tools", "-"], value: { name: "orders-get" } }],
            "Add a tool",
          ),
      },
      {
        id: "notify-customer",
        label: "notify-customer",
        icon: "workflows",
        group: "Sub-workflows",
        disabled: "Needs a platform with sub-workflows",
        apply: () => undefined,
      },
    ],
  },
];

export function registerAgent(): void {
  registerSubNodes("agent", agentSlots);
  registerParameters({ kind: "agent", load: async () => AgentParameters });
  registerParameters({
    kind: "agent",
    subNode: "tools",
    wide: true,
    load: async () => AgentParameters,
  });
}

// A field that is always written, even when removed: the language requires its
// key, so removing it writes the default. Leaving the member out deletes the key.
export const keepsItsKey: ParamSpec = {
  id: "concurrency",
  path: ["concurrency"],
  type: "number",
  label: "Run at most",
  default: 2,
  whenRemoved: "default",
};
export const deletesItsKey: ParamSpec = {
  ...keepsItsKey,
  whenRemoved: "delete",
};
export const omitsTheChoice: ParamSpec = {
  id: "description",
  path: ["description"],
  type: "text",
  label: "Description",
};
export const removedChoices: NonNullable<ParamSpec["whenRemoved"]>[] = [
  "delete",
  "default",
  // @ts-expect-error a field either deletes its key or writes the default
  "clear",
];

// What loop and call parameters read.
export function loopItem(ctx: NdvContext, loop: string): Json | null {
  const sample: SampleContext | null = ctx.sample.context(["items"]);
  return sample?.loops?.[loop]?.item ?? sample?.item ?? null;
}
export const pickedIteration = (ctx: NdvContext): InstanceKey | null =>
  ctx.instanceKey;
export const calleeInterface = (kind: KindContext, uses: string): Json | null =>
  kind.workflowContract(uses);

// A kind that can run in an environment answers with the body of
// POST {ENV}/step-tests, typed as the platform's request without the draft
// identity, or says why it can't. The kind sets everything about the step,
// including the timeout (an AI agent may ask for up to 900 seconds); the editor
// adds the saved draft's ID and revision when it sends the request.
export const actionRealExecution: RealExecutionSupport = {
  request: (ctx) => {
    const uses = ctx.step["uses"];
    if (typeof uses !== "string" || !uses)
      return { blocked: "Choose an action first." };
    const input = ctx.sample.resolvedInput();
    if (input.state !== "available")
      return { blocked: "Run the previous steps first." };
    return {
      body: {
        kind: ctx.step.kind,
        step_id: ctx.step.id,
        uses,
        input: input.value,
        timeout_seconds: 120,
      },
    };
  },
};

type Equal<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2
    ? true
    : false;
type KindBody = Extract<
  ReturnType<RealExecutionSupport["request"]>,
  { body: unknown }
>["body"];

// The two fields the hook leaves out are the platform request's own names.
export const hookLeavesOutTheDraftIdentity: Equal<
  Exclude<keyof StepTestRequest, keyof KindBody>,
  "draft_id" | "draft_revision"
> = true;

// What the editor sends: the kind's body and the draft it is testing.
export const sentRequest = (
  body: KindBody,
  draft: Pick<StepTestRequest, "draft_id" | "draft_revision">,
): StepTestRequest => ({ ...body, ...draft });

export const kindCannotSetTheDraft: KindBody = {
  kind: "action",
  step_id: "check-customer",
  input: null,
  timeout_seconds: 120,
  // @ts-expect-error draft_id is the editor's, not the kind's
  draft_id: "0f8f2a10-3c4d-4e5f-8a9b-1c2d3e4f5a6b",
};

// @ts-expect-error timeout_seconds belongs to the kind's body
export const kindMustSetTheTimeout: KindBody = {
  kind: "action",
  step_id: "a",
  input: null,
};

// Parameters and Settings forms, the way the AI step forms write them.
export const aiTaskForms: Pick<StepKindDescriptor, "form" | "settings"> = {
  form: () => ({
    fields: [
      {
        id: "model",
        path: ["profile"],
        type: "model",
        label: "Model",
        required: true,
      },
      {
        id: "prompt",
        path: ["prompt"],
        type: "multiline",
        label: "Prompt",
        required: true,
        mapping: "both",
        templateCapable: true,
      },
    ],
    options: [
      {
        id: "context",
        path: ["context"],
        type: "json",
        label: "Context",
        mapping: "both",
      },
      {
        id: "temperature",
        path: ["temperature"],
        type: "number",
        label: "Temperature",
        min: 0,
        max: 2,
        showWhen: (step) => step["profile"] !== undefined,
        hiddenReason: () => "Choose a model first",
      },
    ],
  }),
  settings: () => ({
    fields: [
      {
        id: "timeout",
        path: ["spec", "llmProfiles", "support", "timeoutSeconds"],
        scope: "workflow",
        type: "duration",
        label: "Timeout",
        units: ["seconds", "minutes"],
      },
    ],
  }),
};
export const tableChoices = (ctx: NdvContext): Promise<Choice[]> =>
  ctx.catalog
    .tables()
    .then((rows) => rows.map((row) => ({ value: row.uses, label: row.title })));
export const ownedMethod = (kind: KindContext, uses: string): Json | null =>
  kind.ownedAction?.(uses) ?? null;
