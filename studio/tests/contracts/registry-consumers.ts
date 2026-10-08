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
// Compile-only: registrations written the way an AI step module and a loop or
// call module write them. `npm run check` type-checks this file; nothing runs it.
import { Component, input } from "@angular/core";
import {
  NDV_REGISTRY_VERSION,
  registerParameters,
  registerSubNodes,
  type Edit,
  type InstanceKey,
  type Json,
  type KindContext,
  type NdvContext,
  type ParameterComponent,
  type RealExecutionSupport,
  type SampleContext,
  type SubNodeSlotSpec,
} from "../../src/app/editor/ndv/registry";
import type { StepTestRequest } from "../../src/app/editor/state/execution";

// A registry version 2 fails here first.
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
// POST {ENV}/step-tests, typed as the platform's request, or says why it can't.
// The draft fields and the timeout belong to the saved draft, not to the step.
const savedDraft = {
  draft_id: "0f8f2a10-3c4d-4e5f-8a9b-1c2d3e4f5a6b",
  draft_revision: 12,
  timeout_seconds: 120,
};
export const actionRealExecution: RealExecutionSupport = {
  request: (ctx) => {
    const uses = ctx.step["uses"];
    if (typeof uses !== "string" || !uses)
      return { blocked: "Choose an action first." };
    const input = ctx.sample.resolvedInput();
    if (input.state !== "available")
      return { blocked: "Run the previous steps first." };
    const body: StepTestRequest = {
      kind: ctx.step.kind,
      step_id: ctx.step.id,
      uses,
      input: input.value,
      ...savedDraft,
    };
    return { body };
  },
};
