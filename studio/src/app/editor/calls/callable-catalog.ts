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
// The workflows a step may call: the project's published versions ("In this
// project"), then the drafts on this computer, each name's versions newest
// first. A version that doesn't allow this workflow stays in the list,
// disabled with the compiler's sentence; one Studio can't evaluate stays
// choosable with a warning. The platform decides at validation, publication
// and activation; this list only saves the person a refused call. Who may
// call what mirrors the platform's allows_caller (compiler/ir.py), pinned by
// studio/tests/fixtures/callable-corpus.json.
import { isJsonObject, type JsonObject } from "../../forms/core/json";
import type { CatalogLockState } from "./catalog-lock";

/** The most versions the list shows; typing narrows it. */
export const CALLABLE_LIMIT = 500;

/** A draft on this computer, as its index names it, with its document read. */
export interface LocalCallee {
  name: string;
  version: string;
  /** The parsed definition, or null when Studio can't read it. */
  document: unknown;
}
/** One version the Workflow field offers. */
export interface CallableItem {
  /** `name@version`, what the step's `uses` holds. */
  uses: string;
  name: string;
  version: string;
  /** Published in this project, or a draft on this computer. */
  source: "project" | "local";
  inputSchema: JsonObject | null;
  outputSchema: JsonObject | null;
  /** Why this workflow can't call it, in the compiler's words. */
  disabled?: string;
  /** Why Studio can't tell; the version stays choosable. */
  warning?: string;
}
export interface CallableList {
  items: CallableItem[];
  /** More versions match than the list shows. */
  truncated: boolean;
  /** Why the project's versions are missing, or null. */
  note: string | null;
}

/** The compiler's CALL_NOT_CALLABLE sentence (compiler/calls.py CALL_MESSAGES). */
export const notCallable = (caller: string): string =>
  `This workflow version does not allow calls from ${caller}; add ${caller} to its callable.allowedCallers in a new version.`;
const cantTell = (name: string, version: string) =>
  `Studio can't tell whether ${name} ${version} allows this workflow.`;

/** Whether a workflow's `callable` field lets `caller` (a workflow name) call it: the platform's allows_caller. */
export function allowsCaller(callable: unknown, caller: string): boolean {
  if (!isJsonObject(callable)) return false;
  const allowed = callable["allowedCallers"];
  return (
    allowed === undefined ||
    allowed === null ||
    (Array.isArray(allowed) && allowed.includes(caller))
  );
}

const specOf = (document: unknown): JsonObject | null =>
  isJsonObject(document) && isJsonObject(document["spec"])
    ? document["spec"]
    : null;
const schemaOf = (value: unknown): JsonObject | null =>
  isJsonObject(value) ? value : null;

function item(
  name: string,
  version: string,
  source: CallableItem["source"],
  document: unknown,
  caller: string,
): CallableItem {
  const uses = `${name}@${version}`;
  const spec = specOf(document);
  const found: CallableItem = {
    uses,
    name,
    version,
    source,
    inputSchema: schemaOf(spec?.["inputSchema"]),
    outputSchema: schemaOf(spec?.["outputSchema"]),
  };
  if (!spec) return { ...found, warning: cantTell(name, version) };
  const callable = spec["callable"];
  const openToAll =
    isJsonObject(callable) &&
    (callable["allowedCallers"] === undefined ||
      callable["allowedCallers"] === null);
  // Without the caller's name, only a version open to every workflow is known.
  if (!caller && !openToAll)
    return { ...found, warning: cantTell(name, version) };
  return allowsCaller(callable, caller)
    ? found
    : { ...found, disabled: notCallable(caller) };
}

/** One SemVer identifier: numbers by value and before words, words by code point. */
function identifier(x: string, y: string): number {
  const xn = /^\d+$/.test(x),
    yn = /^\d+$/.test(y);
  if (xn && yn) return x.length - y.length || (x < y ? -1 : x > y ? 1 : 0);
  if (xn !== yn) return xn ? -1 : 1;
  return x < y ? -1 : x > y ? 1 : 0;
}
/** SemVer precedence: a pre-release comes before its release; build labels don't count. */
export function compareVersions(a: string, b: string): number {
  const parts = (value: string) => {
    const plain = value.split("+")[0];
    const dash = plain.indexOf("-");
    return {
      core: (dash < 0 ? plain : plain.slice(0, dash)).split("."),
      pre: dash < 0 ? [] : plain.slice(dash + 1).split("."),
    };
  };
  const left = parts(a),
    right = parts(b);
  for (let i = 0; i < 3; i++) {
    const order = identifier(left.core[i] ?? "0", right.core[i] ?? "0");
    if (order) return order;
  }
  if (!left.pre.length || !right.pre.length)
    return right.pre.length - left.pre.length;
  for (let i = 0; i < Math.max(left.pre.length, right.pre.length); i++) {
    if (left.pre[i] === undefined) return -1;
    if (right.pre[i] === undefined) return 1;
    const order = identifier(left.pre[i], right.pre[i]);
    if (order) return order;
  }
  return 0;
}
/** By name, then newest version first. */
const byNameThenNewest = (a: CallableItem, b: CallableItem) =>
  a.name < b.name
    ? -1
    : a.name > b.name
      ? 1
      : compareVersions(b.version, a.version);

/** Why the project's published versions are missing: not connected, no catalog read, or a failed read; null otherwise. */
export function lockNote(lock: CatalogLockState): string | null {
  if (lock.kind === "offline")
    return "Connect to a platform to call published workflows.";
  if (lock.kind === "forbidden")
    return "You can't read this project's published workflows here.";
  if (lock.kind === "failed") return lock.message;
  return null;
}

/**
 * The versions `caller` (the calling workflow's name) may choose: the
 * project's from the catalog lock, then the drafts on this computer. A draft
 * with the same `name@version` as a published version is left out, since the
 * platform resolves `uses` to the published one. `query` keeps the versions
 * whose name and version hold every word typed.
 */
export function callables(request: {
  caller: string;
  lock: CatalogLockState;
  drafts: readonly LocalCallee[];
  query?: string;
}): CallableList {
  const { caller, lock } = request;
  const project =
    lock.kind === "ready"
      ? [...lock.lock.workflows.values()].map((workflow) =>
          item(
            workflow.name,
            workflow.version,
            "project",
            workflow.document,
            caller,
          ),
        )
      : [];
  const seen = new Set(project.map((entry) => entry.uses));
  const local: CallableItem[] = [];
  for (const draft of request.drafts) {
    if (!draft.name || !draft.version) continue;
    const uses = `${draft.name}@${draft.version}`;
    if (seen.has(uses)) continue;
    seen.add(uses);
    local.push(
      item(draft.name, draft.version, "local", draft.document, caller),
    );
  }
  const words = (request.query ?? "")
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);
  const matching = [
    ...project.sort(byNameThenNewest),
    ...local.sort(byNameThenNewest),
  ].filter((entry) => {
    const label = `${entry.name} ${entry.version}`.toLowerCase();
    return words.every((word) => label.includes(word));
  });
  return {
    items: matching.slice(0, CALLABLE_LIMIT),
    truncated: matching.length > CALLABLE_LIMIT,
    note: lockNote(lock),
  };
}
