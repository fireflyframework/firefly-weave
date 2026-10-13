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
// Steps that call an action the workflow owns: inserting one brings its
// action (or the email or file action the workflow already owns for that
// operation) and its connection slot, and a file step can move to another
// file server. Each runs inside one undo step; when any part is refused,
// the caller's batch leaves the workflow and its canvas as they were.
import type { StructuredCanvasAdapter, Step, Workflow } from "../../../model";
import {
  pruneOwnedRecipes,
  type CanvasSidecar,
} from "../../state/canvas-sidecar";
import { uniqueIdentifier } from "../params/identifiers";
import { allSteps, slotsOf } from "../params/slots";
import { findStep } from "../step-walk";
import {
  FILE_CONNECTORS,
  fileAction,
  fileConnector,
  type CommonAction,
} from "./common-actions";
import {
  HTTP_CONNECTOR,
  nameOfUses,
  ownedName,
  ownedUses,
  type ConnectorRecipe,
  type OwnedRecipe,
} from "./owned-actions";
import { ownedActionsOf, unpublished, withOwnedAction } from "./owned-store";
import { forkEdited } from "./owned-versions";

/**
 * The canvas sidecar without the owned actions no step calls any more:
 * steps inside paths and branches count, and so do steps not placed yet.
 */
export function pruneOwned(
  canvas: CanvasSidecar,
  definition: Workflow,
  unplaced: readonly Step[] = [],
): CanvasSidecar {
  const used = new Set(
    [...allSteps(definition), ...unplaced].map((step) =>
      nameOfUses(String(step["uses"] ?? "")),
    ),
  );
  return pruneOwnedRecipes(canvas, used);
}

/**
 * Action names a new owned action must not take because they name another
 * document: actions Studio holds a document for (published, imported or
 * built) and the workspace's published actions, at any version. Documents
 * of actions this workflow owns don't count.
 */
export function reservedActionNames(
  contracts: ReadonlyMap<string, unknown>,
  versions: readonly Record<string, unknown>[],
  canvas: CanvasSidecar,
): Set<string> {
  const owned = new Set(Object.keys(ownedActionsOf(canvas)));
  return new Set([
    ...[...contracts.keys()].filter((uses) => !owned.has(uses)).map(nameOfUses),
    ...versions
      .map((row) => row["name"])
      .filter((name): name is string => typeof name === "string"),
  ]);
}

/** Names taken for a new owned action: owned ones, every action a step calls, and the reserved ones. */
export function takenNames(
  model: StructuredCanvasAdapter,
  reserved: ReadonlySet<string> = new Set(),
): Set<string> {
  return new Set([
    ...Object.keys(ownedActionsOf(model.canvas)).map(nameOfUses),
    ...[...allSteps(model.definition), ...model.unplaced]
      .map((step) => step["uses"])
      .filter((uses): uses is string => typeof uses === "string" && !!uses)
      .map(nameOfUses),
    ...reserved,
  ]);
}

const sameOperation = (a: OwnedRecipe, b: ConnectorRecipe) =>
  a.kind === "connector" &&
  a.connector === b.connector &&
  a.action === b.action;

/**
 * Inserts a common action's step at `owner`/`index` and selects it; returns
 * its ID. Call an API gets an action of its own; email and file steps share
 * the workflow's owned action for the same operation when there is one. The
 * step uses its connector's slot when the workflow declares exactly one.
 * Runs as one undo step, and leaves nothing behind when refused.
 */
export function insertOwnedStep(
  model: StructuredCanvasAdapter,
  action: CommonAction,
  owner: string,
  index: number | undefined,
  reserved: ReadonlySet<string> = new Set(),
): string {
  return model.batch(() => {
    const workflow = model.definition;
    let recipe = action.recipe();
    if (recipe.kind === "connector" && FILE_CONNECTORS.has(recipe.connector))
      recipe = { ...recipe, connector: fileConnector(workflow) };
    // Actions whose steps were deleted since the workflow opened go now, so
    // their names are free again; Undo brings them back with this insert.
    model.canvas = pruneOwned(model.canvas, workflow, model.unplaced);
    const id = uniqueIdentifier(action.idPrefix, model.stepIdSet());
    const connectorRecipe = recipe.kind === "connector" ? recipe : null;
    const shared = connectorRecipe
      ? Object.entries(ownedActionsOf(model.canvas)).find(([, existing]) =>
          sameOperation(existing, connectorRecipe),
        )?.[0]
      : undefined;
    const uses =
      shared ??
      ownedUses(
        ownedName(
          workflow.metadata.name,
          recipe.kind === "http" ? id : action.idPrefix,
          takenNames(model, reserved),
        ),
      );
    if (!shared) model.canvas = withOwnedAction(model.canvas, uses, recipe);
    const connector = connectorRecipe?.connector ?? HTTP_CONNECTOR;
    const slots = slotsOf(workflow).filter(
      (slot) => slot.connector === connector,
    );
    model.insert("action", owner, index, {
      id,
      uses,
      ...(slots.length === 1 ? { connection: slots[0].name } : {}),
    });
    return id;
  });
}

/**
 * Points a step that calls its own file action at a slot on another file
 * server, inside the caller's batch; returns the action the step calls now.
 * The step joins the workflow's owned action for that server and operation
 * when there is one; otherwise its action moves with it when no other step
 * calls it, and is copied for this step when others do, so they keep theirs.
 * Retry, timeout, description and the step's input go along. A published
 * action that moves starts its next version (`versionsOf` names versions the
 * workspace already has); a copy starts unpublished. Throws, before
 * changing anything, for an action the workflow doesn't own and for a slot
 * that isn't a file server's.
 */
export function moveFileStep(
  model: StructuredCanvasAdapter,
  stepId: string,
  slotName: string,
  reserved: ReadonlySet<string> = new Set(),
  versionsOf: (name: string) => readonly string[] = () => [],
): string {
  const step = findStep(model.definition.spec.steps, (s) => s.id === stepId);
  if (!step) throw Error("Select a placed step.");
  const uses = String(step["uses"] ?? "");
  const recipe = ownedActionsOf(model.canvas)[uses];
  if (recipe?.kind !== "connector" || !FILE_CONNECTORS.has(recipe.connector))
    throw Error(
      "Only a file step's own action can move to another file server.",
    );
  const slot = slotsOf(model.definition).find((s) => s.name === slotName);
  if (!slot || !FILE_CONNECTORS.has(slot.connector))
    throw Error("Choose a slot on a file server.");
  let next = uses;
  if (slot.connector !== recipe.connector) {
    const moved: ConnectorRecipe = { ...recipe, connector: slot.connector };
    const joined = Object.entries(ownedActionsOf(model.canvas)).find(
      ([name, existing]) => name !== uses && sameOperation(existing, moved),
    )?.[0];
    const callers = [...allSteps(model.definition), ...model.unplaced].filter(
      (s) => s["uses"] === uses,
    ).length;
    if (joined) next = joined;
    else if (callers === 1)
      model.canvas = withOwnedAction(model.canvas, uses, moved);
    else {
      next = ownedUses(
        ownedName(
          model.definition.metadata.name,
          fileAction(recipe.action)?.idPrefix ?? recipe.action,
          takenNames(model, reserved),
        ),
      );
      model.canvas = withOwnedAction(model.canvas, next, unpublished(moved));
    }
  }
  model.update(
    stepId,
    JSON.stringify({ ...step, uses: next, connection: slotName }),
  );
  // A published action moved to another server is a change: it starts its next version.
  return forkEdited(model, versionsOf, new Set([next]))[0]?.to ?? next;
}
