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
// What an AI task still needs, read without loading the step details code:
// the canvas asks for a model until the step's AI settings exist, and lists
// the compiler's messages about the step as that setup instead of as errors.
import type { Step, Workflow } from "../../../model";

export const SETUP = "Choose a model";

export type Record_ = Record<string, unknown>;
export const isRecord = (value: unknown): value is Record_ =>
  !!value && typeof value === "object" && !Array.isArray(value);

/** A workflow document that can hold AI tasks (the Source text may not be one yet). */
export function isAiWorkflow(value: unknown): value is Workflow {
  return isRecord(value) && isRecord(value["spec"]);
}
export function profilesOf(workflow: Workflow): Record_ {
  const profiles = workflow.spec["llmProfiles"];
  return isRecord(profiles) ? profiles : {};
}
/** The settings an AI task reads: its own, or those it shares. */
export function profileNameOf(step: Step): string {
  return typeof step["profile"] === "string" ? step["profile"] : "";
}
export function profileOf(workflow: Workflow, step: Step): Record_ | null {
  const profile = profilesOf(workflow)[profileNameOf(step)];
  return isRecord(profile) ? profile : null;
}
/** What an AI task still needs on the canvas: a model, until its settings exist. */
export function aiTaskSetup(workflow: Workflow, step: Step): string {
  return step.kind === "llm" && !profileOf(workflow, step) ? SETUP : "";
}
/**
 * True while an AI task has no model: the canvas lists the compiler's
 * messages about it (its missing settings and slot, an empty prompt) as the
 * setup it needs, under "Choose a model", instead of as errors.
 */
export function deferredToSetup(workflow: Workflow, stepId: string): boolean {
  const step = findStep(workflow.spec.steps, stepId);
  return !!step && !!aiTaskSetup(workflow, step);
}
function findStep(value: unknown, id: string): Step | null {
  if (Array.isArray(value)) {
    for (const item of value) {
      const found = findStep(item, id);
      if (found) return found;
    }
  } else if (isRecord(value)) {
    if (value["id"] === id && typeof value["kind"] === "string")
      return value as Step;
    for (const child of Object.values(value)) {
      const found = findStep(child, id);
      if (found) return found;
    }
  }
  return null;
}
