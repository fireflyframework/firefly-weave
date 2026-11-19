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
// The published workflows that call this one, read from the project's catalog
// lock. Calls are found as the platform finds them (`calledReferenceSites`):
// call steps, the workflows a step in a transaction calls to undo it, and the
// workflows an AI agent may call as tools. One row per calling workflow: its
// newest published version that calls any version of this one. With them,
// the rules and words of the Called by a workflow trigger: who may call it
// (`spec.callable`), each caller's line, and the question before removing it.
import { compareVersions, allowsCaller } from "./callable-catalog";
import type { CatalogLock } from "./catalog-lock";
import { calledReferenceSites } from "../activation/callee-pins";
import { getAt, isJsonObject, parsePointer } from "../../forms/core/json";

/** How a caller calls: it waits for the result, it doesn't, or an AI agent calls it as a tool. */
export type CallerMode = "waits" | "doesn't wait" | "tool";

export interface Caller {
  name: string;
  version: string;
  /** The ID of the step that calls (the AI agent step for a tool). */
  step: string;
  mode: CallerMode;
  /** Whether this workflow's `callable` allows the caller; the compiler refuses its next activation otherwise. */
  allowed: boolean;
}

const UNDO = new Set(["confirm", "cancel", "compensate"]);

/** The calling step and how it calls, from where the reference sits. */
function site(
  document: unknown,
  path: string,
): { step: string; mode: CallerMode } | null {
  const segments = parsePointer(path);
  if (!segments || segments.length < 2) return null;
  const last = segments[segments.length - 1];
  let owner: string[];
  let mode: CallerMode | null = null;
  if (last === "workflow") {
    // `…/tools/<n>/workflow`: the agent step owns the tool.
    owner = segments.slice(0, -3);
    mode = "tool";
  } else if (UNDO.has(segments[segments.length - 2])) {
    // `…/<confirm|cancel|compensate>/uses`: an undo call always waits.
    owner = segments.slice(0, -2);
    mode = "waits";
  } else owner = segments.slice(0, -1);
  const step = getAt(document, owner);
  if (!isJsonObject(step) || typeof step["id"] !== "string") return null;
  mode ??= step["mode"] === "detach" ? "doesn't wait" : "waits";
  return { step: step["id"], mode };
}

/**
 * The workflows of `lock` that call `workflow` (any version), each with the
 * step that calls and how, and whether `callable` allows it. Allowed callers
 * first, then the ones the compiler would refuse; each by name.
 */
export function calledBy(input: {
  workflow: string;
  callable: unknown;
  lock: CatalogLock;
}): Caller[] {
  const newest = new Map<string, Caller>();
  for (const locked of input.lock.workflows.values()) {
    const kept = newest.get(locked.name);
    if (kept && compareVersions(kept.version, locked.version) >= 0) continue;
    const call = calledReferenceSites(locked.document).find(
      (found) => found.reference.split("@")[0] === input.workflow,
    );
    const where = call && site(locked.document, call.path);
    if (!where) continue;
    newest.set(locked.name, {
      name: locked.name,
      version: locked.version,
      ...where,
      allowed: allowsCaller(input.callable, locked.name),
    });
  }
  return [...newest.values()].sort(
    (a, b) =>
      Number(b.allowed) - Number(a.allowed) || a.name.localeCompare(b.name),
  );
}

/** The platform's rule for a resource name, as `allowedCallers` holds them. */
const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
/** The most names `allowedCallers` holds. */
export const MAX_CALLERS = 100;

/** "a, b and c". */
const listed = (names: readonly string[]) =>
  names.length < 2
    ? (names[0] ?? "")
    : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;

/** "Step notify" or "AI agent assistant": the step that calls. */
const stepOf = (caller: Caller) =>
  `${caller.mode === "tool" ? "AI agent" : "Step"} ${caller.step}`;

/** The line under a caller's name; `short` on a phone. */
export function callerLine(caller: Caller, short: boolean): string {
  if (!caller.allowed)
    return short
      ? "Calls this workflow but isn't allowed. Its next activation will fail."
      : `${stepOf(caller)} calls this workflow, but ${caller.name} isn't one of the workflows allowed. Its next activation will fail.`;
  const how =
    caller.mode === "tool"
      ? short
        ? "tool"
        : "workflow tool"
      : caller.mode === "waits" && !short
        ? "waits for the result"
        : caller.mode;
  return `${stepOf(caller)} · ${how}`;
}

export const callersCaption = (count: number) =>
  `${count} published ${count === 1 ? "workflow" : "workflows"}`;

/** Why `name` can't join the workflows allowed to call, or null. */
export function callerProblem(
  name: string,
  allowed: readonly string[],
): string | null {
  if (!NAME.test(name))
    return "Use letters, numbers, dots, underscores or hyphens, starting with a letter or number.";
  if (allowed.includes(name)) return `${name} can already call this workflow.`;
  if (allowed.length >= MAX_CALLERS)
    return `Up to ${MAX_CALLERS} workflows can be named here.`;
  return null;
}

const namesOf = (callable: unknown): string[] =>
  isJsonObject(callable) && Array.isArray(callable["allowedCallers"])
    ? callable["allowedCallers"].filter(
        (name): name is string => typeof name === "string",
      )
    : [];

/** `callable` with `name` allowed: the first name turns Any workflow into Only these workflows. */
export function withCaller(callable: unknown, name: string) {
  return {
    ...(isJsonObject(callable) ? callable : {}),
    allowedCallers: [...namesOf(callable), name],
  };
}

/** `callable` without `name`; null for the last one, which would let any workflow call. */
export function withoutCaller(callable: unknown, name: string) {
  const names = namesOf(callable).filter((kept) => kept !== name);
  return names.length
    ? { ...(isJsonObject(callable) ? callable : {}), allowedCallers: names }
    : null;
}

/**
 * The question before the trigger goes (and `spec.callable` with it):
 * the callers when Studio knows them (null when it couldn't read them), each
 * with its step, and that the settings' switch turns off too.
 */
export function removalQuestion(
  workflow: string,
  callers: readonly Caller[] | null,
) {
  const names = (callers ?? []).map((caller) => caller.name);
  const lead =
    callers === null
      ? [
          "Workflows that call this one fail at their next activation until they stop calling it.",
        ]
      : names.length === 1
        ? [
            `${names[0]} calls this workflow. Its next activation will fail until it stops calling it.`,
          ]
        : names.length
          ? [
              `${listed(names)} call this workflow. Their next activations will fail until they stop calling it.`,
            ]
          : [];
  const rows = (callers ?? []).map(
    (caller) =>
      `${caller.name} ${caller.version} · ${caller.mode === "tool" ? "AI agent" : "step"} ${caller.step}`,
  );
  return {
    title: "Remove Called by a workflow?",
    message: [
      ...lead,
      ...rows,
      `Other workflows can no longer call ${workflow} from its next version. Callable by other workflows in the workflow settings turns off too.`,
    ].join("\n\n"),
    confirmLabel: "Remove",
    danger: true,
    startOnCancel: true,
  };
}

/**
 * The question before Only these workflows gives way to Any workflow in this
 * project: it widens who may call, so it is asked, starting on Cancel.
 */
export function anyCallerQuestion(workflow: string, names: readonly string[]) {
  return {
    title: `Let any workflow in this project call ${workflow}?`,
    message: `Only ${listed(names)} can call it now. Studio keeps ${names.length === 1 ? "this name" : "these names"} while ${workflow} is open, so choosing Only these workflows again brings ${names.length === 1 ? "it" : "them"} back.`,
    confirmLabel: "Let any workflow call it",
    startOnCancel: true,
  };
}
