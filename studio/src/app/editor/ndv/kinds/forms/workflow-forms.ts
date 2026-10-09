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
// Forms over the workflow document itself: the Manual form trigger (its
// input fields), End (the workflow result) and the workflow settings.
import type { FormSpec } from "../../registry";
import { durationParam } from "./shared";

export const triggerForm = (): FormSpec => ({
  fields: [
    {
      id: "inputSchema",
      path: ["spec", "inputSchema"],
      scope: "workflow",
      type: "schema",
      label: "Input fields",
      default: { type: "object" },
      whenRemoved: "default",
      hint: "What a person fills in, or a caller sends, to start a run.",
    },
  ],
});

export const endForm = (): FormSpec => ({
  fields: [
    {
      id: "result",
      path: ["spec", "output"],
      scope: "workflow",
      type: "keyValue",
      label: "Result fields",
      mapping: "both",
      default: {},
      whenRemoved: "default",
      addLabel: "Add result field",
    },
  ],
  options: [
    {
      id: "resultSchema",
      path: ["spec", "outputSchema"],
      scope: "workflow",
      type: "schema",
      label: "Result schema",
      default: { type: "object" },
      whenRemoved: "default",
      hint: "Describe the result for callers.",
    },
  ],
});

export const workflowSettingsForm = (): FormSpec => ({
  fields: [
    {
      id: "name",
      path: ["metadata", "name"],
      scope: "workflow",
      type: "text",
      label: "Name",
      required: true,
      identifier: true,
      default: "untitled-workflow",
      whenRemoved: "default",
    },
    {
      id: "version",
      path: ["metadata", "version"],
      scope: "workflow",
      type: "text",
      label: "Version",
      required: true,
      default: "1.0.0",
      whenRemoved: "default",
      placeholder: "1.0.0",
      hint: "Use a version like 1.2.0.",
    },
  ],
  options: [
    durationParam("timeout", ["spec", "timeoutSeconds"], "Stop runs after", {
      scope: "workflow",
      hint: "A run that takes longer stops with an error.",
    }),
    {
      id: "callable",
      path: ["spec", "callable"],
      scope: "workflow",
      type: "boolean",
      label: "Callable by other workflows",
      feature: "flow.callWorkflow",
      choices: [{ value: {}, label: "Callable" }],
      // Callable is present as an empty object, so off is the default; without
      // it the stored {} would read as never added.
      default: false,
      hint: "Other workflows can call this one and wait for its result.",
    },
  ],
});
