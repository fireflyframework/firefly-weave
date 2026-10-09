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
// The AI task, until the AI step form replaces it: a model (the workflow's
// AI profile) and a prompt; context and the action and connection behind it
// are options.
import type { Step } from "../../../../model";
import type { FormSpec, KindContext } from "../../registry";
import { isRecord } from "../shared";

export function llmForm(_step: Step, ctx: KindContext): FormSpec {
  const profiles = isRecord(ctx.workflow.spec["llmProfiles"])
    ? ctx.workflow.spec["llmProfiles"]
    : {};
  return {
    fields: [
      {
        id: "model",
        path: ["profile"],
        type: "select",
        label: "Model",
        required: true,
        choices: Object.entries(profiles).map(([name, profile]) => {
          const model = isRecord(profile) ? profile["model"] : undefined;
          return {
            value: name,
            label:
              typeof model === "string" && model ? `${name} · ${model}` : name,
          };
        }),
        description:
          "The AI profile this step uses: its provider, model and options.",
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
        type: "keyValue",
        label: "Context",
        mapping: "both",
        default: {},
        addLabel: "Add context field",
      },
      {
        id: "action",
        path: ["uses"],
        type: "text",
        label: "AI action version",
        default: "weave-agentic-generate@1.0.0",
        hint: "The published action that runs the AI worker.",
      },
      {
        id: "connection",
        path: ["connection"],
        type: "connection",
        label: "AI connection",
        default: "ai",
      },
    ],
  };
}
