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
// The actions this workflow owns, by the `uses` its steps write
// (`<name>@1.0.0`). The canvas sidecar keeps them: each name in
// `ownedActions`, and what it does in `actionRecipes`.
//
// The setters pass on the canvas sidecar's own errors: a plain, user-worded
// Error for a name or recipe a canvas file can't hold, a name already taken,
// a 201st action, or a canvas past its size cap. Callers show that message
// and leave the canvas as it was.
import {
  ownedRecipes,
  renameOwnedRecipe,
  withOwnedRecipe,
  type ActionRecipe,
  type CanvasSidecar,
} from "../../state/canvas-sidecar";
import {
  OWNED_VERSION,
  nameOfUses,
  ownedUses,
  type OwnedRecipe,
} from "./owned-actions";

const owned = (uses: string): boolean => uses.endsWith(`@${OWNED_VERSION}`);
const NOT_OWNED = `Only an action at version ${OWNED_VERSION} can belong to this workflow.`;

export function ownedActionsOf(
  canvas: CanvasSidecar,
): Record<string, OwnedRecipe> {
  return Object.fromEntries(
    Object.entries(ownedRecipes(canvas)).map(([name, recipe]) => [
      ownedUses(name),
      recipe as unknown as OwnedRecipe,
    ]),
  );
}

/**
 * The canvas with the recipe of `uses` set, or the owned action removed
 * (null); the same canvas when nothing changes. A published action (any
 * other version) is never removed here, and can't be given a recipe.
 */
export function withOwnedAction(
  canvas: CanvasSidecar,
  uses: string,
  recipe: OwnedRecipe | null,
): CanvasSidecar {
  if (!owned(uses)) {
    if (recipe) throw Error(NOT_OWNED);
    return canvas;
  }
  return withOwnedRecipe(
    canvas,
    nameOfUses(uses),
    recipe as unknown as ActionRecipe | null,
  );
}

/**
 * An owned action renamed, recipe and name together. An action that isn't
 * owned (another version) isn't ours to rename, so the canvas stays as it is.
 */
export function renameOwnedAction(
  canvas: CanvasSidecar,
  from: string,
  to: string,
): CanvasSidecar {
  if (!owned(from)) return canvas;
  if (!owned(to)) throw Error(NOT_OWNED);
  return renameOwnedRecipe(canvas, nameOfUses(from), nameOfUses(to));
}

/** True when the step's action is one this workflow owns. */
export const usesOwning = (canvas: CanvasSidecar, stepUses: unknown): boolean =>
  typeof stepUses === "string" &&
  owned(stepUses) &&
  Object.hasOwn(ownedRecipes(canvas), nameOfUses(stepUses));
