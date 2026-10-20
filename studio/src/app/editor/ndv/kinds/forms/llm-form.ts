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

// The AI task: a model, chosen connection first, and a prompt. The model's
// settings live in the workflow (`llmProfiles`), so the options that tune them
// are workflow fields and wait until a model is chosen; the timeout is on
// Settings.
import type { Step } from "../../../../model";
import type { Choice, FormSpec, KindContext, ParamSpec } from "../../registry";
import {
  DEFAULT_MAX_TOKENS,
  TEXT_RESULT,
  ownSettingsName,
  profileNameOf,
  profileOf,
  profilesOf,
} from "../ai-task-model";
import { durationParam } from "./shared";

const REASONING: Choice[] = [
  { value: "none", label: "None" },
  { value: "react", label: "Reason and act" },
  { value: "chain_of_thought", label: "Chain of thought" },
  { value: "plan_and_execute", label: "Plan and execute" },
  { value: "reflexion", label: "Reflect and revise" },
  { value: "tree_of_thoughts", label: "Tree of thoughts" },
  { value: "goal_decomposition", label: "Break the goal down" },
];

const settingsPath = (step: Step, ...rest: string[]) => [
  "spec",
  "llmProfiles",
  profileNameOf(step),
  ...rest,
];
/** Options that tune the step's settings appear once a model is chosen. */
const afterModel: Pick<ParamSpec, "showWhen" | "hiddenReason" | "whenHidden"> =
  {
    showWhen: (step, ctx) => !!profileOf(ctx.workflow, step),
    hiddenReason: () => "Choose a model first.",
    whenHidden: "keep",
  };

function shareChoices(step: Step, ctx: KindContext): Choice[] {
  const profiles = profilesOf(ctx.workflow);
  const own = ownSettingsName(ctx.workflow, step);
  return [
    { value: own, label: "This step only" },
    ...Object.entries(profiles)
      .filter(([name]) => name !== own)
      .map(([name, profile]) => {
        const model = (profile as Record<string, unknown> | null)?.["model"];
        return {
          value: name,
          label: typeof model === "string" ? `${name} · ${model}` : name,
        };
      }),
  ];
}

export function llmForm(step: Step, ctx: KindContext): FormSpec {
  return {
    fields: [
      {
        id: "model",
        path: settingsPath(step, "model"),
        scope: "workflow",
        type: "model",
        label: "Model",
        required: true,
        hint: "Models are grouped by AI connection. The connection becomes the slot the step uses.",
        description:
          "The model that answers. Choosing one also chooses its AI connection; Studio adds the connection slot and the step's AI settings.",
      },
      {
        id: "prompt",
        path: ["prompt"],
        type: "multiline",
        label: "Prompt",
        required: true,
        default: "",
        whenRemoved: "default",
        mapping: "both",
        templateCapable: true,
        placeholder: "Summarize {{ the ticket text }} in two sentences.",
      },
    ],
    options: [
      {
        id: "context",
        path: ["context"],
        type: "keyValue",
        label: "Context",
        mapping: "both",
        default: {},
        whenRemoved: "default",
        addLabel: "Add context field",
        hint: "Data the model reads with the prompt.",
      },
      {
        id: "result",
        path: settingsPath(step, "outputSchema"),
        scope: "workflow",
        type: "schema",
        label: "Structured result",
        default: { ...TEXT_RESULT },
        whenRemoved: "default",
        hint: "Fields the model fills in. Without them, the result is text.",
        ...afterModel,
      },
      {
        id: "maxTokens",
        path: settingsPath(step, "options", "max_tokens"),
        scope: "workflow",
        type: "number",
        label: "Max output tokens",
        default: DEFAULT_MAX_TOKENS,
        min: 1,
        max: 32768,
        whenRemoved: "default",
        hint: "The longest answer the model may write.",
        ...afterModel,
      },
      {
        id: "temperature",
        path: settingsPath(step, "options", "temperature"),
        scope: "workflow",
        type: "number",
        label: "Temperature",
        min: 0,
        max: 2,
        placeholder: "Model default",
        hint: "Lower answers are more predictable.",
        ...afterModel,
      },
      {
        id: "reasoning",
        path: settingsPath(step, "reasoning", "pattern"),
        scope: "workflow",
        type: "select",
        label: "Reasoning pattern",
        default: "none",
        choices: REASONING,
        hint: "Extra model calls that work the answer out first.",
        ...afterModel,
      },
      {
        id: "share",
        path: ["profile"],
        type: "select",
        label: "Share settings with other AI steps",
        // Settings only this step uses read as not shared, whatever their name.
        default: ownSettingsName(ctx.workflow, step),
        whenRemoved: "default",
        choices: shareChoices(step, ctx),
        hint: "Steps that share settings use the same model and options.",
      },
    ],
  };
}

export function llmSettings(step: Step): FormSpec {
  return {
    fields: [
      durationParam(
        "timeout",
        settingsPath(step, "timeoutSeconds"),
        "Timeout",
        {
          scope: "workflow",
          default: 120,
          max: 600,
          hint: "The step stops with an error after this time.",
          ...afterModel,
        },
      ),
    ],
  };
}
