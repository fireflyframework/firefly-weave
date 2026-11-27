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
// What may undo a step in a transaction, as an undo picker lists it: the
// project's published actions, then its workflows, each name's newest version
// first, with the fact a row ends with and why one can't be chosen. Only
// published versions: an undo step names one. The platform decides at
// validation; the list saves the person a refused choice.
import { isJsonObject } from "../../forms/core/json";
import { callables, compareVersions } from "../calls/callable-catalog";
import type { CatalogLockState } from "../calls/catalog-lock";
import { undoFact, undoPickerReason, type UndoCandidate } from "./undo";

/** The most actions, and the most workflows, the list shows; typing narrows it. */
const LIMIT = 500;

export interface UndoTarget {
  uses: string;
  title: string;
  kind: UndoCandidate["kind"];
  group: "Actions" | "Workflows";
  aside?: string;
  /** Why Studio can't tell whether the version may be chosen; it stays choosable. */
  note?: string;
  disabled?: string;
}
/** An action version and its document, or null while Studio doesn't have it. */
export interface KnownAction {
  uses: string;
  contract: unknown;
}

const specOf = (document: unknown): Record<string, unknown> =>
  isJsonObject(document) && isJsonObject(document["spec"])
    ? document["spec"]
    : {};

const split = (uses: string): [string, string] => {
  const at = uses.lastIndexOf("@");
  return [uses.slice(0, at), uses.slice(at + 1)];
};
const byNameThenNewest = (a: KnownAction, b: KnownAction) => {
  const [left, leftVersion] = split(a.uses);
  const [right, rightVersion] = split(b.uses);
  return left < right
    ? -1
    : left > right
      ? 1
      : compareVersions(rightVersion, leftVersion);
};

function row(
  uses: string,
  group: UndoTarget["group"],
  candidate: UndoCandidate,
  refused?: string,
  note?: string,
): UndoTarget {
  const aside = undoFact(candidate);
  const disabled = refused ?? undoPickerReason(candidate);
  return {
    uses,
    title: split(uses).join(" "),
    kind: candidate.kind,
    group,
    ...(aside ? { aside } : {}),
    ...(note ? { note } : {}),
    ...(disabled ? { disabled } : {}),
  };
}

/**
 * The list from the project's catalog. Connected but unable to read it whole,
 * Studio lists the published actions it knows, with what it has of each.
 */
export function undoTargets(
  state: CatalogLockState,
  caller: string,
  published: () => readonly KnownAction[],
): UndoTarget[] {
  if (state.kind === "offline" || state.kind === "forbidden") return [];
  const lock = state.kind === "ready" ? state.lock : null;
  const actions: KnownAction[] = lock
    ? [...lock.actions.values()].map(({ uses, document }) => ({
        uses,
        contract: document,
      }))
    : [...published()];
  const workflows = callables({ caller, lock: state, drafts: [] }).items;
  return [
    ...actions
      .sort(byNameThenNewest)
      .slice(0, LIMIT)
      .map(({ uses, contract }) => {
        const sideEffect = specOf(contract)["sideEffect"];
        return row(uses, "Actions", {
          kind: "action",
          ...(typeof sideEffect === "string" ? { sideEffect } : {}),
        });
      }),
    ...workflows.slice(0, LIMIT).map((item) => {
      const limit = specOf(lock?.workflows.get(item.uses)?.document)[
        "timeoutSeconds"
      ];
      return row(
        item.uses,
        "Workflows",
        {
          kind: "callWorkflow",
          ...(typeof limit === "number" ? { timeoutSeconds: limit } : {}),
        },
        item.disabled,
        item.warning,
      );
    }),
  ];
}

/** Why the project's actions and workflows aren't offered, or null. */
export function undoNote(state: CatalogLockState): string | null {
  if (state.kind === "offline")
    return "Connect to a platform to choose published actions and workflows.";
  if (state.kind === "forbidden")
    return "You can't read this project's published actions and workflows here.";
  return state.kind === "failed" ? state.message : null;
}
