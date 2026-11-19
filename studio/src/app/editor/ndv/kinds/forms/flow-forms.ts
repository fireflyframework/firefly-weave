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
// Decision, Parallel, Loop over items, Wait, Wait for signal and Stop with
// error. A decision's paths and its Otherwise sit in one Paths field; a
// parallel's branches are one list; a loop shows its list and keeps its
// limits behind Add option; timeouts live on Settings.
import type { Step } from "../../../../model";
import { loopHeaderParts } from "../../../canvas/loop-frame";
import type { FormSpec, Json, ParamSpec } from "../../registry";
import { durationParam } from "./shared";

/** A path or branch of a new step: no steps yet, and an empty result. */
const emptyBranch = (): Json => ({ steps: [], output: { literal: {} } });

const pathResult = (id: string, path: (string | number)[]): ParamSpec => ({
  id,
  path,
  type: "json",
  label: "Path result",
  mapping: "both",
  default: {},
  whenRemoved: "default",
  hint: "What later steps read as this step's output when this path runs.",
});

export const switchForm = (): FormSpec => ({
  fields: [
    {
      id: "paths",
      path: [],
      type: "fields",
      label: "Paths",
      labelHidden: true,
      hint: "Paths are checked from top to bottom. Each path is an output on the canvas, labeled with its condition.",
      children: () => [
        {
          id: "cases",
          path: ["cases"],
          type: "list",
          label: "Paths",
          minItems: 1,
          default: [emptyBranch()],
          whenRemoved: "default",
          addLabel: "Add path",
          item: {
            id: "case",
            path: [],
            type: "fields",
            label: "Path",
            children: () => [
              {
                id: "case.when",
                path: ["when"],
                type: "conditions",
                label: "Runs when",
                required: true,
              },
              pathResult("case.output", ["output"]),
            ],
          },
        },
        {
          id: "otherwise",
          path: ["default"],
          type: "fields",
          label: "Otherwise",
          default: emptyBranch(),
          whenRemoved: "default",
          trailingCard: true,
          hint: "Runs when no path applies.",
          children: () => [pathResult("default.output", ["default", "output"])],
        },
      ],
    },
  ],
});

const branchNames = (step: Step): string[] =>
  step["branches"] && typeof step["branches"] === "object"
    ? Object.keys(step["branches"] as object)
    : [];

export const parallelForm = (): FormSpec => ({
  fields: [
    {
      id: "branches",
      path: ["branches"],
      type: "list",
      label: "Branches",
      minItems: 1,
      default: { first: emptyBranch(), second: emptyBranch() },
      whenRemoved: "default",
      addLabel: "Add branch",
      hint: "Branches run at the same time; the next step starts when all are done.",
      item: {
        id: "branch",
        path: [],
        type: "text",
        label: "Branch",
        identifier: true,
      },
    },
  ],
  options: [
    {
      id: "concurrency",
      path: ["concurrency"],
      type: "number",
      label: "Run at most",
      min: 1,
      default: 2,
      whenRemoved: "default",
      hint: "How many branches run at the same time.",
    },
    {
      id: "branch-results",
      path: [],
      type: "fields",
      label: "Branch results",
      hint: "What later steps read from each branch.",
      children: (step) =>
        branchNames(step).map((name) => ({
          id: `branches.${name}.output`,
          path: ["branches", name, "output"],
          type: "json" as const,
          label: `${name} result`,
          mapping: "both" as const,
          default: {},
          whenRemoved: "default" as const,
        })),
    },
  ],
});

/**
 * A loop's list and its limits. The language writes every limit out, so
 * removing one writes its default back and the option reads as not added.
 * Bounds above 1 are the platform's: the compiler reports them on the field.
 */
export const loopForm = (): FormSpec => ({
  fields: [
    {
      id: "items",
      path: ["items"],
      type: "formula",
      label: "Items",
      mapping: "mapped",
      required: true,
      requiredText: "Choose the list to loop over.",
      hint: "The list to run the steps for, one item at a time.",
    },
  ],
  options: [
    {
      id: "concurrency",
      path: ["concurrency"],
      type: "number",
      label: "At a time",
      min: 1,
      default: 1,
      whenRemoved: "default",
      hint: "How many items run at the same time. 1 runs them one after another.",
    },
    {
      id: "maxItems",
      path: ["maxItems"],
      type: "number",
      label: "Up to",
      min: 1,
      default: 1000,
      whenRemoved: "default",
      // The platform fails the run when the list holds more; it leaves nothing out.
      hint: "The most items the list may hold. A longer list fails the run.",
    },
    {
      id: "collect",
      path: ["collect"],
      type: "select",
      label: "Results",
      choices: [
        { value: "all", label: "Keep every result" },
        { value: "nonNull", label: "Skip empty results" },
      ],
      default: "all",
      whenRemoved: "default",
      hint: "Leave out items whose result is empty, or keep every result.",
    },
    {
      id: "output",
      path: ["body", "output"],
      type: "json",
      label: "Result per item",
      mapping: "both",
      // The item itself: a mapping, never the literal object.
      defaultExpression: { ref: "/item" },
      whenRemoved: "default",
      hint: "What the loop returns for each item. Without this option, the item itself.",
    },
  ],
});

/** "input.orders · up to 4 at a time": the frame's header without "For each item in"; empty without a list. */
export function loopSummary(step: Step): string {
  const { items, suffix } = loopHeaderParts(step);
  return items
    ? [items, suffix.replace(/^· /, "")].filter(Boolean).join(" · ")
    : "";
}

export const waitForm = (): FormSpec => ({
  fields: [
    durationParam("duration", ["durationSeconds"], "Wait for", {
      required: true,
      default: 60,
    }),
  ],
});

export const signalForm = (): FormSpec => ({
  fields: [
    {
      id: "name",
      path: ["name"],
      type: "text",
      label: "Signal name",
      required: true,
      requiredText: "Choose a signal name; names are unique in a workflow.",
      identifier: true,
      placeholder: "payment-received",
      hint: "Names are unique in a workflow.",
    },
  ],
  options: [
    {
      id: "payload",
      path: ["payloadSchema"],
      type: "schema",
      label: "Payload fields",
      default: { type: "object" },
      whenRemoved: "default",
      hint: "Describe the data the signal brings.",
    },
  ],
});

export const signalSettings = (): FormSpec => ({
  fields: [
    durationParam("timeout", ["timeoutSeconds"], "Wait at most", {
      required: true,
      default: 3600,
      hint: "The step fails if nothing arrives by then.",
    }),
  ],
});

export const failForm = (): FormSpec => ({
  fields: [
    {
      id: "code",
      path: ["code"],
      type: "text",
      label: "Error code",
      required: true,
      identifier: true,
      default: "business-error",
      placeholder: "customer-not-found",
    },
    {
      id: "message",
      path: ["message"],
      type: "multiline",
      label: "Message",
      required: true,
      requiredText: "Write a message the run will report.",
      hint: "Write a message the run will report.",
    },
  ],
});
