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
// A workflow's platform event trigger is declared in the definition:
// `spec.eventTrigger` names the events that may start it and, in `from`, the
// workflows whose transactions it reacts to (any workflow in the project
// when `from` is left out). Studio reads and writes that declaration, words
// it for the tile, says what the platform would refuse, warns when the
// workflow reacts to its own transactions, and tells whether the workflow's
// input already takes the platform event.
import { isJsonObject } from "../../forms/core/json";
import type { Workflow } from "../../model";
import {
  EVENT_CHOICES,
  EVENT_TYPES,
  isPlatformEventInput,
  isPlatformEventType,
  type PlatformEventType,
} from "./platform-event";

export interface EventDeclaration {
  events: PlatformEventType[];
  /** The source workflows; null for any workflow in the project. */
  from: string[] | null;
}

/** A new trigger reacts to a transaction that needs attention, from any workflow. */
export const DEFAULT_DECLARATION: EventDeclaration = {
  events: ["transaction.in_doubt"],
  from: null,
};

/** The most source workflows a declaration may name, as the platform allows. */
export const MAX_SOURCES = 100;
/** How many times in a row the platform lets reactions start reactions. */
const MAX_DEPTH = 3;
const WORKFLOW_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

/**
 * The workflow's declaration, or null when it doesn't react to platform
 * events. A `from` that isn't a list reads as no workflow, never as any.
 */
export function readDeclaration(workflow: Workflow): EventDeclaration | null {
  const declared = workflow.spec["eventTrigger"];
  if (!isJsonObject(declared)) return null;
  const events = Array.isArray(declared["events"])
    ? declared["events"].filter(isPlatformEventType)
    : [];
  const from = !("from" in declared)
    ? null
    : Array.isArray(declared["from"])
      ? declared["from"].filter(
          (name): name is string => typeof name === "string",
        )
      : [];
  return { events, from };
}

/**
 * The workflow with `spec.eventTrigger` written from the declaration, or
 * removed for null: each event once, in the platform's order, and `from` left
 * out for any workflow. Every other part of the workflow stays as it was, and
 * an existing declaration keeps its place in `spec`.
 */
export function withDeclaration(
  workflow: Workflow,
  declaration: EventDeclaration | null,
): Workflow {
  const spec = { ...workflow.spec };
  if (declaration === null) {
    delete spec["eventTrigger"];
    return { ...workflow, spec };
  }
  const events = EVENT_TYPES.filter((type) =>
    declaration.events.includes(type),
  );
  spec["eventTrigger"] =
    declaration.from === null
      ? { events }
      : { events, from: [...declaration.from] };
  return { ...workflow, spec };
}

/** "Needs attention · from move-between-systems", "3 events · any workflow". */
export function eventSubtitle(declaration: EventDeclaration): string {
  const { events, from } = declaration;
  const what =
    events.length === 0
      ? "No events chosen"
      : events.length === 1
        ? EVENT_CHOICES[events[0]!].short
        : `${events.length} events`;
  const where =
    from === null
      ? "any workflow"
      : from.length === 0
        ? "no workflow named"
        : from.length === 1
          ? `from ${from[0]}`
          : `from ${from.length} workflows`;
  return `${what} · ${where}`;
}

/** What the platform would refuse, by field; empty when nothing is wrong. */
export function eventProblems(declaration: EventDeclaration): {
  events?: string;
  from?: string;
} {
  const problems: { events?: string; from?: string } = {};
  if (declaration.events.length === 0)
    problems.events = "Choose at least one event.";
  const from = declaration.from;
  if (from === null) return problems;
  const twice = from.find((name, index) => from.indexOf(name) !== index);
  const invalid = from.find((name) => !WORKFLOW_NAME.test(name));
  if (from.length === 0)
    problems.from =
      "Name at least one workflow, or choose Any workflow in this project.";
  else if (from.length > MAX_SOURCES)
    problems.from = `Up to ${MAX_SOURCES} workflows can be named here.`;
  else if (invalid !== undefined)
    problems.from = `“${invalid}” isn't a workflow name. Use letters, numbers, dots, underscores or hyphens, starting with a letter or number.`;
  else if (twice !== undefined) problems.from = `${twice} is named twice.`;
  return problems;
}

/**
 * The warning when the workflow names itself in `from`: it then reacts to its
 * own transactions, until the platform stops the chain. Any workflow leaves
 * its own transactions out.
 */
export function loopWarning(
  declaration: EventDeclaration,
  workflowName: string,
): string | null {
  return declaration.from?.includes(workflowName)
    ? `${workflowName} reacts to its own transactions too. Reactions stop after ${MAX_DEPTH} in a row.`
    : null;
}

/**
 * What adding the trigger does to the workflow's input: set it to the
 * platform event when there is none, keep it when it is the platform event,
 * and ask before replacing any other input.
 */
export function eventInputFit(inputSchema: unknown): "set" | "keep" | "ask" {
  if (
    inputSchema === undefined ||
    inputSchema === null ||
    (isJsonObject(inputSchema) && Object.keys(inputSchema).length === 0)
  )
    return "set";
  return isPlatformEventInput(inputSchema) ? "keep" : "ask";
}
