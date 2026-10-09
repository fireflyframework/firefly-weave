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
// Renaming a step from its details: typing is free; committing turns the
// text into an unused ID and renames the step, every reference to it, its
// note and, for Call an API, the action it owns, as one undo step.
import type { StructuredCanvasAdapter } from "../../model";
import { ownedName, ownedUses } from "./owned/owned-actions";
import { ownedActionsOf, renameOwnedAction } from "./owned/owned-store";
import { normalizeIdentifier, uniqueIdentifier } from "./params/identifiers";
import { allSteps, findStep } from "./step-walk";

export interface RenameResult {
  id: string;
  references: number;
  uses: { from: string; to: string } | null;
}

/** The ID the text becomes, or null when it is empty or the current one. */
export function renameTarget(
  text: string,
  current: string,
  taken: ReadonlySet<string>,
): string | null {
  const base = normalizeIdentifier(text);
  if (!base || base === current) return null;
  return uniqueIdentifier(
    base,
    new Set([...taken].filter((id) => id !== current)),
  );
}

/** "Saved as check-customer-2" while the typed text isn't stored as typed. */
export function renameHint(
  text: string,
  current: string,
  taken: ReadonlySet<string>,
): string {
  const target = renameTarget(text, current, taken);
  return target && target !== text ? `Saved as ${target}` : "";
}

/**
 * Renames a step, as one undo step. `model.renameStep` rewrites the
 * references and moves the step's note in the canvas sidecar; a Call an API
 * step that is the only one using its owned action also takes the action
 * along (`<workflow>.<step>`), so the two keep matching. When any part is
 * refused (a name the canvas can't hold, a step that is gone) nothing is
 * changed, and the error reaches the caller as it was thrown.
 */
export function renameWithExtras(
  model: StructuredCanvasAdapter,
  oldId: string,
  text: string,
): RenameResult {
  const target = renameTarget(text, oldId, model.stepIdSet());
  if (!target) return { id: oldId, references: 0, uses: null };
  return model.batch(() => {
    const references = model.renameStep(oldId, target);
    const step = findStep(model.definition.spec.steps, (s) => s.id === target);
    const fromUses = typeof step?.["uses"] === "string" ? step["uses"] : "";
    const owned = ownedActionsOf(model.canvas);
    let uses: RenameResult["uses"] = null;
    if (step && owned[fromUses]?.kind === "http") {
      const sharedBy = [
        ...allSteps(model.definition.spec.steps),
        ...model.unplaced,
      ].filter((s) => s["uses"] === fromUses).length;
      if (sharedBy === 1) {
        const others = new Set(
          Object.keys(owned)
            .filter((name) => name !== fromUses)
            .map((name) => name.slice(0, name.lastIndexOf("@"))),
        );
        const toUses = ownedUses(
          ownedName(model.definition.metadata.name, target, others),
        );
        model.update(target, JSON.stringify({ ...step, uses: toUses }));
        // renameStep already moved the step's note in the canvas sidecar.
        model.canvas = renameOwnedAction(model.canvas, fromUses, toUses);
        uses = { from: fromUses, to: toUses };
      }
    }
    return { id: target, references, uses };
  });
}
