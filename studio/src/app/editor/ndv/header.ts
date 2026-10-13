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
// The words in step details' header and About this step: the subtitle under
// the step name, how an action runs, what it changes, and the guide link.
import { stepSummary } from "../../designer/canvas-summary";
import { decode, get } from "../../forms/core/binding";
import { getAt, isJsonObject } from "../../forms/core/json";
import type { Step } from "../../model";
import {
  EMAIL_CONNECTOR,
  nameOfUses,
  recipeOf,
  type OwnedRecipe,
} from "./owned/owned-actions";
import type { Json, KindContext } from "./registry";

export const DOCS_BASE =
  "https://fireflyframework.github.io/firefly-weave/guides/studio-step-reference/";

const fileLabels: Record<string, string> = {
  list: "List files",
  read: "Read a file",
  download: "Download a file",
  write: "Write a file",
  move: "Move a file",
  delete: "Delete a file",
};

const isEmail = (recipe: OwnedRecipe): boolean =>
  recipe.kind === "connector" &&
  recipe.connector.startsWith(`${nameOfUses(EMAIL_CONNECTOR)}@`);

/** The Add a step name of an owned action. */
export function ownedEntryLabel(recipe: OwnedRecipe): string {
  if (recipe.kind === "http") return "Call an API";
  if (isEmail(recipe))
    return recipe.action === "reply" ? "Reply to an email" : "Send email";
  return Object.hasOwn(fileLabels, recipe.action)
    ? fileLabels[recipe.action]
    : recipe.action;
}

/** How many people a Send email step writes to, as the subtitle says it. */
function recipients(step: Step): string {
  const to = get(decode(step["with"] ?? { literal: {} }), ["to"]);
  if (to?.kind === "ref" || to?.kind === "formula") return "mapped recipients";
  if (to?.kind !== "array" || !to.items.length) return "no recipients yet";
  return to.items.length === 1
    ? "1 recipient"
    : `${to.items.length} recipients`;
}

export function headerSubtitle(
  step: Step,
  ctx: KindContext,
  kindLabel: string,
): string {
  const recipe = step.kind === "action" ? recipeOf(step, ctx) : null;
  if (recipe?.kind === "http") return "Call an API · HTTP request";
  if (recipe?.kind === "connector") {
    const label = ownedEntryLabel(recipe);
    if (isEmail(recipe))
      return recipe.action === "reply"
        ? `${label} · reply in conversation`
        : `${label} · ${recipients(step)}`;
    const node = get(decode(step["with"] ?? { literal: {} }), ["path"]);
    const path = node?.kind === "value" ? node.value : undefined;
    return typeof path === "string" && path ? `${label} · ${path}` : label;
  }
  if (step.kind === "switch")
    return `${kindLabel} · the first path that applies runs`;
  const summary = stepSummary(step, ctx.workflow);
  return summary ? `${kindLabel} · ${summary}` : kindLabel;
}

/** How an action runs, from its document: the connector and request, or the worker task. */
export function implementationText(document: Json | null): string {
  const implementation = getAt(document, ["spec", "implementation"]);
  if (!isJsonObject(implementation))
    return "Not known until the action's details load";
  if (implementation["kind"] === "worker")
    return [
      "Worker task",
      String(implementation["taskType"] ?? ""),
      String(implementation["taskVersion"] ?? ""),
    ]
      .filter(Boolean)
      .join(" ");
  const config = isJsonObject(implementation["config"])
    ? implementation["config"]
    : {};
  const request = config["method"]
    ? `${String(config["method"])} ${String(config["path"] ?? "")}`.trim()
    : String(implementation["action"] ?? "");
  return [String(implementation["uses"] ?? ""), request]
    .filter(Boolean)
    .join(" · ");
}

export function sideEffectText(sideEffect: string): string {
  switch (sideEffect) {
    case "read_only":
      return "Reads data · safe to retry";
    case "idempotent":
      return "Repeating it changes nothing more · safe to retry";
    case "idempotency_key":
      return "Sends an idempotency key · safe to retry";
    case "non_idempotent":
      return "Changes data · never retried automatically";
    default:
      return "Not known until the action's details load";
  }
}

const anchors: Record<string, string> = {
  action: "configure-call-an-action",
  decisionTable: "decision-table",
  llm: "ai-task",
  transform: "transform",
  switch: "decision",
  parallel: "parallel",
  wait: "wait-for-time",
  signal: "wait-for-signal",
  humanTask: "human-task",
  fail: "fail",
  $trigger: "manual-form-trigger",
};

export function docsUrl(kind: string, recipe: OwnedRecipe | null): string {
  const anchor = recipe
    ? recipe.kind === "http"
      ? "call-an-api"
      : "email-and-file-steps"
    : Object.hasOwn(anchors, kind)
      ? anchors[kind]
      : "configure-each-step";
  return `${DOCS_BASE}#${anchor}`;
}
