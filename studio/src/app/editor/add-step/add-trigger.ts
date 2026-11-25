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
// Adding a trigger choice: the choice joins the canvas, and the definition
// changes with it when the definition declares the trigger (Called by a
// workflow, Platform event). Platform event also brings the workflow's input:
// the platform event, which Studio asks about before it replaces another one.
import type { DialogOptions } from "../../dialog";
import { fieldsOf } from "../../forms/core/resolve";
import { schemaTypes, typeIcon, typeLabel } from "../../forms/core/scope";
import { isJsonObject } from "../../forms/core/json";
import type { Workflow } from "../../model";
import type { TriggerKind } from "../canvas/layout-ltr";
import {
  choicesBefore,
  freshId,
  MAX_TRIGGERS,
  randomHex,
  triggerIntents,
  withTriggerIntents,
  type CanvasSidecar,
} from "../state/canvas-sidecar";
import {
  DEFAULT_DECLARATION,
  eventInputFit,
  readDeclaration,
  withDeclaration,
} from "../triggers/event-trigger";
import { PLATFORM_EVENT_INPUT } from "../triggers/platform-event";

export interface TriggerChange {
  canvas: CanvasSidecar;
  /** The new trigger's ID. */
  id: string;
  /** The definition when the choice changes it (Called by a workflow, Platform event), else null. */
  workflow: Workflow | null;
}

/**
 * The canvas, and the definition when it changes, with a trigger of `kind`.
 * Platform event takes the platform event as the workflow's input, but
 * another input only when `replaceInput` says the person chose to.
 */
export function addTrigger(
  canvas: CanvasSidecar,
  workflow: Workflow,
  kind: TriggerKind,
  hasSteps: boolean,
  random: () => string = randomHex,
  replaceInput = false,
): TriggerChange {
  const list = triggerIntents(canvas);
  const has = (wanted: TriggerKind) =>
    list.some((intent) => intent.kind === wanted);
  if (kind === "manual" && (has("manual") || (!list.length && hasSteps)))
    throw Error("This workflow already starts from its input form.");
  if (kind === "called" && has("called"))
    throw Error("Other workflows can already call this one.");
  if (kind === "event" && has("event"))
    throw Error("This workflow already starts from platform events.");
  const base = choicesBefore(canvas, hasSteps, random);
  if (base.length >= MAX_TRIGGERS)
    throw Error(`A workflow can have at most ${MAX_TRIGGERS} triggers.`);
  const id = freshId(
    "trigger",
    new Set(base.map((intent) => intent.id)),
    random,
  );
  return {
    canvas: withTriggerIntents(canvas, [...base, { id, kind }]),
    id,
    workflow: declared(workflow, kind, replaceInput),
  };
}

/** The definition that declares the trigger, or null when it stays as it is. */
function declared(
  workflow: Workflow,
  kind: TriggerKind,
  replaceInput: boolean,
): Workflow | null {
  if (kind === "called") {
    if (isJsonObject(workflow.spec["callable"])) return null;
    const changed = structuredClone(workflow);
    changed.spec["callable"] = {};
    return changed;
  }
  if (kind !== "event") return null;
  const changed = withDeclaration(
    structuredClone(workflow),
    readDeclaration(workflow) ?? DEFAULT_DECLARATION,
  );
  const fit = eventInputFit(changed.spec["inputSchema"]);
  if (fit === "set" || (fit === "ask" && replaceInput))
    changed.spec["inputSchema"] = { ...PLATFORM_EVENT_INPUT };
  return changed;
}

/** The most fields the question names; it counts the rest. */
const NAMED_FIELDS = 6;

/**
 * The question before the platform event replaces another input. Both answers
 * add the trigger, so each says what happens to the input; keeping it loses
 * nothing, so the question starts there.
 */
export function inputFitQuestion(inputSchema: unknown): DialogOptions {
  const fields = fieldsOf(inputSchema);
  const items = fields.slice(0, NAMED_FIELDS).map((field) => ({
    icon: typeIcon(schemaTypes(field.schema), typeLabel(field.schema)),
    text: field.key,
  }));
  const more = fields.length - items.length;
  const has = fields.length
    ? `: it has ${fields.length} ${fields.length === 1 ? "field" : "fields"}`
    : "";
  return {
    title: "Replace this workflow's input?",
    message: [
      `Runs that Platform event starts get the platform event as their input. This workflow's input doesn't fit it${has}.`,
      "Replace the input to read the event's fields in later steps. Keep the input to set it up yourself: the trigger can't start runs until the input accepts the platform event.",
    ].join("\n\n"),
    ...(items.length
      ? {
          items: more ? [...items, { text: `and ${more} more` }] : items,
          itemStyle: "chips" as const,
        }
      : {}),
    cancelLabel: "Keep the input",
    confirmLabel: "Replace the input",
    startOnCancel: true,
  };
}
