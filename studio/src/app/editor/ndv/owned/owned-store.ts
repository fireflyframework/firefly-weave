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
// (`<name>@<version>`, 1.0.0 until a published action changes). The canvas
// sidecar keeps them: each name in `ownedActions`, and what it does, with
// its current version and what was published, in `actionRecipes`.
//
// The setters pass on the canvas sidecar's own errors: a plain, user-worded
// Error for a name or recipe a canvas file can't hold, a name already taken,
// a 201st action, or a canvas past its size cap. Callers show that message
// and leave the canvas as it was.
import {
  VERSION_CORE,
  ownedRecipes,
  renameOwnedRecipe,
  withOwnedRecipe,
  type ActionRecipe,
  type CanvasSidecar,
} from "../../state/canvas-sidecar";
import {
  OWNED_VERSION,
  currentVersion,
  nameOfUses,
  ownedUses,
  versionOfUses,
  type OwnedRecipe,
} from "./owned-actions";

/** The recipe of `uses` when it is the version this workflow calls of an action it owns. */
function listed(canvas: CanvasSidecar, uses: string): OwnedRecipe | undefined {
  const name = nameOfUses(uses);
  const recipe = ownedRecipes(canvas)[name] as unknown as
    | OwnedRecipe
    | undefined;
  return recipe && uses === ownedUses(name, currentVersion(recipe))
    ? recipe
    : undefined;
}
/** The recipe as the canvas keeps it: a version other than 1.0.0 is written down. */
function versioned(recipe: OwnedRecipe, version: string): ActionRecipe {
  const copy = { ...recipe };
  if (version === OWNED_VERSION) delete copy.version;
  else copy.version = version;
  return copy as unknown as ActionRecipe;
}

export function ownedActionsOf(
  canvas: CanvasSidecar,
): Record<string, OwnedRecipe> {
  return Object.fromEntries(
    Object.entries(ownedRecipes(canvas)).map(([name, recipe]) => [
      ownedUses(name, currentVersion(recipe as { version?: unknown })),
      recipe as unknown as OwnedRecipe,
    ]),
  );
}

/**
 * The canvas with the recipe of `uses` set, or the owned action removed
 * (null); the same canvas when nothing changes. `uses` must be the version
 * the workflow calls of an action it owns, or a new name at 1.0.0; any other
 * version is another document, never removed here and never given a recipe.
 */
export function withOwnedAction(
  canvas: CanvasSidecar,
  uses: string,
  recipe: OwnedRecipe | null,
): CanvasSidecar {
  const name = nameOfUses(uses);
  const existing = ownedRecipes(canvas)[name] as unknown as
    | OwnedRecipe
    | undefined;
  const version = existing ? currentVersion(existing) : OWNED_VERSION;
  if (uses !== ownedUses(name, version)) {
    if (!recipe) return canvas;
    throw Error(
      existing
        ? `Only version ${version} of ${name} belongs to this workflow.`
        : `A new action of this workflow starts at version ${OWNED_VERSION}.`,
    );
  }
  return withOwnedRecipe(
    canvas,
    name,
    recipe ? versioned(recipe, version) : null,
  );
}

/** The canvas with an owned action at another version, in its place in the list. */
export function withOwnedVersion(
  canvas: CanvasSidecar,
  name: string,
  version: string,
): CanvasSidecar {
  const recipe = ownedRecipes(canvas)[name] as unknown as
    | OwnedRecipe
    | undefined;
  if (!recipe) throw Error(`${name} isn't an action this workflow owns.`);
  if (!VERSION_CORE.test(version)) throw Error("Use a version such as 1.0.1.");
  return withOwnedRecipe(canvas, name, versioned(recipe, version));
}

/**
 * An owned action renamed, recipe and name together, at the same version.
 * An action that isn't owned at that version isn't ours to rename; a
 * published one keeps its name.
 */
export function renameOwnedAction(
  canvas: CanvasSidecar,
  from: string,
  to: string,
): CanvasSidecar {
  const recipe = listed(canvas, from);
  if (!recipe) return canvas;
  if (versionOfUses(to) !== versionOfUses(from))
    throw Error("A renamed action keeps its version.");
  if (recipe.published) throw Error("A published action keeps its name.");
  return renameOwnedRecipe(canvas, nameOfUses(from), nameOfUses(to));
}

/** True when the step's action is one this workflow owns, at the version it calls. */
export const usesOwning = (canvas: CanvasSidecar, stepUses: unknown): boolean =>
  typeof stepUses === "string" && !!listed(canvas, stepUses);

/** A copy of a recipe for another action: it starts at 1.0.0, unpublished. */
export function unpublished(recipe: OwnedRecipe): OwnedRecipe {
  const copy = { ...recipe };
  delete copy.version;
  delete copy.published;
  return copy;
}
