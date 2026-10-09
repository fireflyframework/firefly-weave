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
// What a registered form must keep, checked for every ready kind by a
// contract test: a ready kind has a Parameters form, field IDs are unique
// within the kind, Fixed | Mapped appears only where the step holds an
// expression, and custom components appear only where Studio allows them.
import type { Step } from "../../model";
import { WORKFLOW_ROOTS, expressionRoot } from "./params/paths";
import type {
  FormSpec,
  KindContext,
  ParamSpec,
  StepKindDescriptor,
} from "./registry";

/** The only custom components: the decision table grid and the AI agent's slot sections. */
export const CUSTOM_COMPONENTS: Readonly<Record<string, readonly string[]>> = {
  decisionTable: ["grid"],
  agent: ["slots", "tools"],
};

/** Every field of a form with its options, children and list items, depth first; item paths are relative. */
export function allParams(
  form: FormSpec | undefined,
  step: Step,
  ctx: KindContext,
): { spec: ParamSpec; relative: boolean }[] {
  const out: { spec: ParamSpec; relative: boolean }[] = [];
  const visit = (spec: ParamSpec, relative: boolean, depth: number) => {
    if (depth > 8) return;
    out.push({ spec, relative });
    if (spec.item) visit(spec.item, true, depth + 1);
    for (const child of spec.children?.(step, ctx) ?? [])
      visit(child, relative, depth + 1);
  };
  for (const spec of [...(form?.fields ?? []), ...(form?.options ?? [])])
    visit(spec, false, 0);
  return out;
}

/** The problems of one kind's forms for one step, in a stable order. */
export function formProblems(
  descriptor: StepKindDescriptor,
  step: Step,
  ctx: KindContext,
): string[] {
  const kind = descriptor.kind;
  if (!descriptor.form) return [`${kind} has no Parameters form.`];
  const roots = descriptor.fields(step).map((field) => field.path);
  const params = [
    ...allParams(descriptor.form(step, ctx), step, ctx),
    ...allParams(descriptor.settings?.(step, ctx), step, ctx),
  ];
  const problems = new Set<string>();
  const seen = new Set<string>();
  const twice = new Set<string>();
  for (const { spec, relative } of params) {
    if (seen.has(spec.id)) twice.add(spec.id);
    seen.add(spec.id);
    const scope = spec.scope ?? "step";
    const expression =
      relative ||
      (scope === "step" && !!expressionRoot(spec.path, roots)) ||
      (scope === "workflow" && !!expressionRoot(spec.path, WORKFLOW_ROOTS));
    if (spec.mapping === "both" && !expression)
      problems.add(
        `${kind} offers Fixed and Mapped for "${spec.id}", which isn't an expression.`,
      );
    if (
      spec.type === "custom" &&
      !(CUSTOM_COMPONENTS[kind] ?? []).includes(spec.component ?? "")
    )
      problems.add(
        `${kind} uses a custom component for "${spec.id}"; only the decision table grid and the AI agent slots may.`,
      );
  }
  return [
    ...[...twice]
      .sort()
      .map((id) => `${kind} uses the field ID "${id}" twice.`),
    ...problems,
  ];
}
