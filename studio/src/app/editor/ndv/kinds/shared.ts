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
// What every built-in descriptor shares: today's names, defaults and canvas
// summaries, and output schemas from the same scope walk later steps see.
import { stepSummary } from "../../../designer/canvas-summary";
import {
  stepKindDescriptions,
  stepKindKeywords,
  stepKindLabels,
} from "../../../designer/step-kinds";
import { stepOutputSchema, type Schema } from "../../../forms/core/scope";
import { createStep, stepIdPrefix, type Kind, type Step } from "../../../model";
import type {
  FieldSpec,
  Json,
  KindContext,
  NodeRole,
  PanelCategory,
  Path,
  StepKindDescriptor,
} from "../registry";

export const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** The `spec` of a definition document, or an empty object. */
export function specOf(document: Json | null): Record<string, unknown> {
  return isRecord(document) && isRecord(document["spec"])
    ? document["spec"]
    : {};
}
export const schemaOf = (value: unknown): Schema | null =>
  isRecord(value) ? value : null;

/** A step's output schema as later steps see it; `{}` when it's any value. */
export function inferredOutput(step: Step, ctx: KindContext): Schema | null {
  return stepOutputSchema(ctx.workflow, step.id, {
    actionOutput: (uses) => specOf(ctx.actionContract(uses))["outputSchema"],
    decisionOutput: (uses) => specOf(ctx.tableContract(uses))["outputSchema"],
  });
}

export const field = (label: string, ...path: Path): FieldSpec => ({
  path,
  label,
});

export type CommonFields = Pick<
  StepKindDescriptor,
  | "kind"
  | "label"
  | "description"
  | "keywords"
  | "icon"
  | "role"
  | "category"
  | "idPrefix"
  | "create"
  | "summary"
  | "outputSchema"
>;
/** Names, defaults, summary and output schema of a built-in kind. */
export function common(
  kind: Kind,
  role: NodeRole,
  category: PanelCategory,
): CommonFields {
  return {
    kind,
    label: stepKindLabels[kind],
    description: stepKindDescriptions[kind],
    keywords: stepKindKeywords[kind],
    icon: kind,
    role,
    category,
    idPrefix: stepIdPrefix[kind],
    create: (id) => createStep(kind, id),
    summary: (step, ctx) => stepSummary(step, ctx.workflow),
    outputSchema: inferredOutput,
  };
}
