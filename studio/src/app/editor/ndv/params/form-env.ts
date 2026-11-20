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
// What a form reads and how it is evaluated, for a step or for the
// workflow: the expression fields of the step, the schemas they target,
// and the read-only reason and added options of this session.
import type { Step, Workflow } from "../../../model";
import { ERROR_PATH } from "../../step-blocks";
import type { Json, KindContext, StepKindDescriptor } from "../registry";
import type { FormEnv } from "./form-model";
import { samePath } from "./paths";
import type { FormSubject } from "./value-io";

/** The `step` that workflow forms pass to `showWhen` and `children`. */
export const WORKFLOW_STAND_IN: Step = { id: "$workflow", kind: "transform" };

export function stepSubject(
  descriptor: StepKindDescriptor,
  step: Step,
  ctx: KindContext,
  action: Json | null,
): FormSubject {
  const fields = descriptor.fields(step);
  // A step with an error path has one more expression: Result on error.
  const errorOutput =
    step[ERROR_PATH] && typeof step[ERROR_PATH] === "object"
      ? [[ERROR_PATH, "output"]]
      : [];
  return {
    step,
    workflow: ctx.workflow,
    action,
    roots: [...fields.map((field) => field.path), ...errorOutput],
    schemaOf: (root) =>
      fields
        .find((field) => samePath(field.path, root))
        ?.expectedSchema?.(step, ctx) ?? undefined,
  };
}

export const workflowSubject = (workflow: Workflow): FormSubject => ({
  step: null,
  workflow,
  action: null,
  roots: [],
});

export const formEnv = (
  subject: FormSubject,
  step: Step,
  ctx: KindContext,
  readOnly: string | null = null,
  added: ReadonlySet<string> = new Set(),
): FormEnv => ({ subject, step, kind: ctx, readOnly, added });
