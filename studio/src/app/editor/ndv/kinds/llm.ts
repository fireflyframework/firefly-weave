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

// The AI task descriptor: a model chosen connection first, a prompt, and
// options that tune the step's AI settings. Its canvas subtitle is the model.
import { registerKind, type StepKindDescriptor } from "../registry";
import { SETUP, profileOf } from "./ai-task-model";
import { llmForm, llmSettings } from "./forms/llm-form";
import { common } from "./shared";

export const llmKind: StepKindDescriptor = {
  ...common("llm", "ai-task", "ai"),
  summary: (step, ctx) => {
    const model = profileOf(ctx.workflow, step)?.["model"];
    return typeof model === "string" && model ? model : SETUP;
  },
  fields: () => [
    { path: ["prompt"], label: "Prompt", templateCapable: true },
    { path: ["context"], label: "Context" },
  ],
  pinnable: true,
  script: "ai-turns",
  form: llmForm,
  settings: llmSettings,
};

export function register(): void {
  registerKind(llmKind);
}
