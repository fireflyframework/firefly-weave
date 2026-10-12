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
// Versions of the actions a workflow owns. An owned action starts at 1.0.0
// and is edited freely until it is published. Studio then records the
// published version and a fingerprint of what its document was built from;
// a later change that would build another document starts the next patch
// version, and every step that calls the action follows, so a published
// version never changes.
import { nextPatchVersion } from "../../../integrations/http-action-model";
import type { StructuredCanvasAdapter, Workflow } from "../../../model";
import type { CanvasSidecar } from "../../state/canvas-sidecar";
import { allSteps } from "../params/slots";
import {
  currentVersion,
  httpBuildRequest,
  httpSource,
  isFrozen,
  nameOfUses,
  versionOfUses,
  type OwnedRecipe,
} from "./owned-actions";
import {
  ownedActionsOf,
  renameOwnedAction,
  withOwnedAction,
  withOwnedVersion,
} from "./owned-store";

export type OwnedModel = Pick<StructuredCanvasAdapter, "definition" | "canvas">;
export interface VersionMove {
  from: string;
  to: string;
}

const sorted = (value: unknown): unknown =>
  Array.isArray(value)
    ? value.map(sorted)
    : value !== null && typeof value === "object"
      ? Object.fromEntries(
          Object.keys(value)
            .sort()
            .map((key) => [
              key,
              sorted((value as Record<string, unknown>)[key]),
            ]),
        )
      : value;
/** JSON with every object's keys in order, so equal values read the same. */
export const stableJson = (value: unknown): string =>
  JSON.stringify(sorted(value));

/** A 53-bit fingerprint of a text, as 14 hexadecimal digits (it tells changes apart; it protects nothing). */
export function inputHash(text: string): string {
  let h1 = 0xdeadbeef;
  let h2 = 0x41c6ce57;
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    h1 = Math.imul(h1 ^ code, 2654435761);
    h2 = Math.imul(h2 ^ code, 1597334677);
  }
  h1 =
    Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^
    Math.imul(h2 ^ (h2 >>> 13), 3266489909);
  h2 =
    Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^
    Math.imul(h1 ^ (h1 >>> 13), 3266489909);
  return (4294967296 * (2097151 & h2) + (h1 >>> 0))
    .toString(16)
    .padStart(14, "0");
}

/**
 * What an owned action's document is built from, without its version: the
 * builder request and retry of an API action (with its step's input keys),
 * the recipe of a connector action.
 */
export function ownedInput(
  definition: Workflow,
  uses: string,
  recipe: OwnedRecipe,
): string {
  if (recipe.kind !== "http") {
    const content: Record<string, unknown> = { ...recipe };
    delete content["version"];
    delete content["published"];
    return stableJson(content);
  }
  const { step, recipe: described } = httpSource(definition, uses, recipe);
  const request = httpBuildRequest(nameOfUses(uses), described, step?.["with"]);
  if (request) delete request["version"];
  return stableJson([request, recipe.retry ?? null]);
}

/** The owned actions the workflow's steps call at a version that isn't published, in step order. */
export function pendingOwned(model: OwnedModel): string[] {
  const owned = ownedActionsOf(model.canvas);
  const pending = new Set<string>();
  for (const step of allSteps(model.definition)) {
    const uses = String(step["uses"] ?? "");
    if (Object.hasOwn(owned, uses) && !isFrozen(owned[uses])) pending.add(uses);
  }
  return [...pending];
}

/** Published owned actions whose document would now differ from the published one. */
export function driftedOwned(
  model: OwnedModel,
  only?: ReadonlySet<string>,
): string[] {
  return Object.entries(ownedActionsOf(model.canvas))
    .filter(
      ([uses, recipe]) =>
        (!only || only.has(uses)) &&
        isFrozen(recipe) &&
        inputHash(ownedInput(model.definition, uses, recipe)) !==
          recipe.published!.input,
    )
    .map(([uses]) => uses);
}

/**
 * Moves an owned action to a new name or version, with every step (placed
 * or not) that calls it. Runs inside the caller's batch.
 */
export function moveOwned(
  model: StructuredCanvasAdapter,
  from: string,
  to: string,
): void {
  if (!Object.hasOwn(ownedActionsOf(model.canvas), from))
    throw Error(`${from} isn't an action this workflow owns.`);
  const name = nameOfUses(from);
  model.canvas =
    nameOfUses(to) === name
      ? withOwnedVersion(model.canvas, name, versionOfUses(to))
      : renameOwnedAction(model.canvas, from, to);
  for (const step of allSteps(model.definition))
    if (step["uses"] === from)
      model.update(step.id, JSON.stringify({ ...step, uses: to }));
  if (model.unplaced.some((step) => step["uses"] === from))
    model.unplaced = model.unplaced.map((step) =>
      step["uses"] === from ? { ...step, uses: to } : step,
    );
}

/**
 * Gives each changed published action (only those in `only`, when given) its
 * next free patch version, inside the caller's batch, so the change and the
 * new version are one undo step. `versionsOf` names versions the workspace
 * already has.
 */
export function forkEdited(
  model: StructuredCanvasAdapter,
  versionsOf: (name: string) => readonly string[],
  only?: ReadonlySet<string>,
): VersionMove[] {
  const moves: VersionMove[] = [];
  for (const uses of driftedOwned(model, only)) {
    const recipe = ownedActionsOf(model.canvas)[uses];
    const name = nameOfUses(uses);
    const to = `${name}@${nextPatchVersion(currentVersion(recipe), [
      ...versionsOf(name),
      recipe.published!.version,
      currentVersion(recipe),
    ])}`;
    moveOwned(model, uses, to);
    moves.push({ from: uses, to });
  }
  return moves;
}

/**
 * The canvas recording that `uses` is published. An API action without a
 * description of its own keeps the one it was published with, so renaming
 * its step changes nothing that was published.
 */
export function markPublished(model: OwnedModel, uses: string): CanvasSidecar {
  const recipe = ownedActionsOf(model.canvas)[uses];
  if (!recipe) return model.canvas;
  const description =
    recipe.kind === "http" && !recipe.description
      ? httpSource(model.definition, uses, recipe).recipe.description
      : undefined;
  const kept: OwnedRecipe = description ? { ...recipe, description } : recipe;
  return withOwnedAction(model.canvas, uses, {
    ...kept,
    published: {
      version: versionOfUses(uses),
      input: inputHash(ownedInput(model.definition, uses, kept)),
    },
  });
}

/** One action's publish key: the same document always sends the same key. */
export const publishKey = (uses: string, yaml: string): string =>
  `owned-${uses.slice(0, 160)}-${inputHash(yaml)}`.slice(0, 200);

/** The versions of `name` among the workspace's published action rows. */
export const publishedVersionsOf = (
  rows: readonly Record<string, unknown>[],
  name: string,
): string[] =>
  rows
    .filter((row) => row["name"] === name)
    .map((row) => String(row["version"]));

/** What the toast says after a change started a new version. */
export const versionMoveText = (move: VersionMove): string =>
  `${nameOfUses(move.from)} ${versionOfUses(move.from)} is published, so this change starts version ${versionOfUses(move.to)}.`;

/** Why the workflow's actions can't be published for this account. */
export const publishRefusal = (purpose: "publish" | "simulate"): string =>
  purpose === "simulate"
    ? "Simulating this workflow publishes the actions of its Call an API, email and file steps first, and your account can't publish actions in this workspace."
    : "Your account can't publish actions in this workspace.";

/** What Save to file adds while some of the workflow's actions aren't published. */
export const ownedExportNote = (count: number): string =>
  count
    ? `These files don't keep the settings of ${count} ${count === 1 ? "action" : "actions"} of this workflow's Call an API, email or file steps yet. Publish the workflow to keep ${count === 1 ? "it" : "them"} in the project.`
    : "";
