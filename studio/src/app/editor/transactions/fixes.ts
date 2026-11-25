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
// What fixes a transaction's finding: its label, what it names for a screen
// reader, and the change it makes. A fix that changes the workflow is one
// undo step; the others open the field, the action or the workflow the
// person decides in. A step without a choice is never given one.
import type { SuggestedChange } from "../../designer/diagnostics-list";
import {
  ownerLabel,
  sequencesOf,
  type Step,
  type StructuredCanvasAdapter,
  type Workflow,
} from "../../model";
import type { Path } from "../ndv/registry";
import { enclosingTransaction } from "./group";
import { heldAt } from "./points";
import { clearUndo, transactionHold, undoChoiceOf } from "./undo";

export type TransactionFix =
  | {
      kind: "suggestion";
      label: "Move to end" | "Set Hold for";
      name: string;
      change: SuggestedChange;
    }
  | {
      kind: "move";
      label: "Move out" | "Move after transaction";
      name: string;
      to: "out" | "after";
    }
  | { kind: "undo"; label: "Choose how it's undone"; name: string }
  | {
      kind: "hold";
      label: "Set Hold for";
      name: string;
      stepId: string;
      path: Path;
    }
  | { kind: "picker"; label: "Open data picker"; name: string }
  | { kind: "openAction"; label: "Open action"; name: string }
  | {
      kind: "openWorkflow";
      label: "Open workflow";
      name: string;
      uses: string;
    }
  | { kind: "clearUndo"; label: "Clear How it's undone"; name: string }
  | { kind: "waitForCall"; label: "Wait for its result"; name: string };

/** What applying a suggested edit to a workflow that changed says too. */
export const CHANGED_SINCE =
  "The workflow changed after this suggestion was made. Wait for the next check, then try again.";
/** Whether a check's findings are about the workflow as it is now. */
export const aboutNow = (host: {
  readonly model: { readonly definition: Workflow };
  readonly diagnosticsDefinition?: unknown;
}): boolean =>
  JSON.stringify(host.model.definition) ===
  JSON.stringify(host.diagnosticsDefinition);

/** Where a finding is, in the workflow it was made for. */
export interface FindingPlace {
  stepId: string;
  /** The field, with the data keys inside an expression. */
  at: Path;
  workflow: Workflow;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isTransaction = (step: Step) => (step.kind as string) === "transaction";

/** A step with the sequence that holds it. */
function placed(
  workflow: Workflow,
  id: string,
): { step: Step; owner: string; index: number } | null {
  for (const [owner, steps] of sequencesOf(workflow)) {
    const index = steps.findIndex((step) => step.id === id);
    if (index >= 0) return { step: steps[index], owner, index };
  }
  return null;
}
const ownerStep = (owner: string) => owner.slice(0, owner.indexOf("/"));

/** The steps around a step, innermost first. */
function around(workflow: Workflow, id: string): Step[] {
  const found: Step[] = [];
  let at = placed(workflow, id);
  while (at && at.owner !== "root") {
    at = placed(workflow, ownerStep(at.owner));
    if (at) found.push(at.step);
  }
  return found;
}

/**
 * Where a move puts a step: after the outermost loop or parallel step that
 * holds it inside its transaction, or after the transaction.
 */
function moveTarget(
  workflow: Workflow,
  id: string,
  to: "out" | "after",
): { owner: string; index: number; past: Step } | null {
  const outer = around(workflow, id);
  const inside = outer.findIndex(isTransaction);
  if (inside < 0) return null;
  const past =
    to === "after"
      ? outer[inside]
      : outer
          .slice(0, inside)
          .reverse()
          .find(({ kind }) => kind === "forEach" || kind === "parallel");
  const at = past && placed(workflow, past.id);
  return at ? { owner: at.owner, index: at.index + 1, past } : null;
}

/**
 * What sets the shortest hold a waiting step finds: the own Hold for of a
 * reservation made before it, or the transaction's.
 */
function shortestHold(
  workflow: Workflow,
  waiting: string,
): { stepId: string; path: Path } | null {
  const at = placed(workflow, waiting);
  const transaction = at && enclosingTransaction(workflow, at.owner);
  if (!transaction) return null;
  const common = transactionHold(transaction);
  const own = (step: Step) =>
    typeof step["holdSeconds"] === "number" ? step["holdSeconds"] : null;
  const [shortest] = heldAt(transaction, waiting).sort(
    (a, b) => (own(a) ?? common) - (own(b) ?? common),
  );
  return shortest && own(shortest) !== null
    ? { stepId: shortest.id, path: ["holdSeconds"] }
    : { stepId: transaction.id, path: ["policy", "holdSeconds"] };
}

/** The step a reordering puts last, and the list it is in, in words. */
function movedToEnd(
  change: SuggestedChange,
  at: FindingPlace,
): { step: string; list: string } | null {
  const before = placed(at.workflow, change.stepId)?.step;
  const after = change.value;
  if (!before || !isRecord(after)) return null;
  const was = sequencesOf({
    ...at.workflow,
    spec: { steps: [before] },
  } as Workflow);
  const now = sequencesOf({
    ...at.workflow,
    spec: { steps: [after as unknown as Step] },
  } as Workflow);
  for (const [owner, steps] of now) {
    const last = steps.at(-1);
    if (!last || was.get(owner)?.at(-1)?.id === last.id) continue;
    return {
      step: last.id,
      list:
        isTransaction(before) && owner === `${before.id}/steps`
          ? `transaction ${before.id}`
          : ownerLabel(owner, new Map([[before.id, before]])),
    };
  }
  return null;
}

/** What calls or runs at a `uses` path of a step: its own, or an undo step's. */
function usesAt(at: FindingPlace): string {
  const step = placed(at.workflow, at.stepId)?.step;
  const holder =
    at.at.length > 1 && step ? step[String(at.at[0])] : (step as unknown);
  return isRecord(holder) && typeof holder["uses"] === "string"
    ? holder["uses"]
    : "";
}

/**
 * The fix of a transaction's finding; `undefined` for any other code, and
 * null where Studio knows none.
 */
export function transactionFix(
  code: string,
  suggestion: SuggestedChange | null,
  place: FindingPlace | null,
): TransactionFix | null | undefined {
  const id = place?.stepId ?? "";
  const named = <L extends string>(label: L) => ({
    label,
    name: id ? `${label}: ${id}` : label,
  });
  switch (code.replace(/^WV-COMP-/, "")) {
    case "TXN_ORDER": {
      if (!suggestion || !place) return null;
      const moved = movedToEnd(suggestion, place);
      return {
        kind: "suggestion",
        label: "Move to end",
        name: moved
          ? `Move to end: Move ${moved.step} to the end of ${moved.list}`
          : "Move to end",
        change: suggestion,
      };
    }
    case "TXN_PIVOT_PLACEMENT":
    case "TXN_AFTER_PIVOT_UNSAFE": {
      const to = code.endsWith("PLACEMENT") ? "out" : "after";
      const target = place && moveTarget(place.workflow, id, to);
      if (!target) return null;
      const label = to === "out" ? "Move out" : "Move after transaction";
      return {
        kind: "move",
        label,
        to,
        name: `${label}: ${moves(id, to, target.past)}`,
      };
    }
    // Never the compiler's own answer: only the builder says a step can't be undone.
    case "TXN_UNDECLARED":
      return { kind: "undo", ...named("Choose how it's undone") };
    case "TXN_HOLD_WAIT": {
      const held = place && shortestHold(place.workflow, id);
      return held
        ? {
            kind: "hold",
            ...held,
            label: "Set Hold for",
            name: `Set Hold for: ${held.stepId}`,
          }
        : null;
    }
    case "TXN_HOLD_PIVOT":
      return suggestion
        ? { kind: "suggestion", ...named("Set Hold for"), change: suggestion }
        : null;
    case "TXN_CANCEL_OUTPUT":
      return { kind: "picker", ...named("Open data picker") };
    case "TXN_UNDO_SIDE_EFFECT":
      return { kind: "openAction", ...named("Open action") };
    case "TXN_CALL_TIMEOUT": {
      const uses = place ? usesAt(place) : "";
      return uses
        ? { kind: "openWorkflow", ...named("Open workflow"), uses }
        : null;
    }
    case "TXN_OUTSIDE":
      return { kind: "clearUndo", ...named("Clear How it's undone") };
    case "TXN_DETACH":
      return { kind: "waitForCall", ...named("Wait for its result") };
    default:
      return undefined;
  }
}

const moves = (id: string, to: "out" | "after", past: Step) =>
  to === "out"
    ? `Move ${id} out of ${past.id}`
    : `Move ${id} after transaction ${past.id}`;

/** What a fix that changes the workflow says it did: "Moved submit out of each."; null for the others. */
export function fixedText(
  workflow: Workflow,
  fix: { kind: string; to?: "out" | "after" },
  id: string,
): string | null {
  const step = placed(workflow, id)?.step;
  if (fix.kind === "clearUndo") return `Cleared how ${id} is undone.`;
  // A call that waits takes part in the transaction: how it is undone is the builder's.
  if (fix.kind === "waitForCall")
    return `${id} waits for its result.${step && undoChoiceOf(step) ? "" : " Choose how it's undone."}`;
  const target =
    fix.kind === "move" && fix.to && moveTarget(workflow, id, fix.to);
  if (!target) return null;
  const moved = moves(id, fix.to!, target.past).replace(/^Move/, "Moved");
  return fix.to === "after" && step && clearUndo(step).length
    ? `${moved} and cleared how it's undone.`
    : `${moved}.`;
}

/** Makes a fix's change in the open workflow, as one undo step; false when it makes none. */
export function applyTransactionFix(
  model: StructuredCanvasAdapter,
  fix: { kind: string; to?: "out" | "after" },
  id: string,
): boolean {
  const step = placed(model.definition, id)?.step;
  if (!step) return false;
  if (fix.kind === "move" && fix.to) {
    const target = moveTarget(model.definition, id, fix.to);
    if (!target) return false;
    model.batch(() => {
      model.move(id, target.owner, target.index);
      // Outside a transaction a step may not say how it is undone.
      if (fix.to === "after") written(model, step, withoutUndo(step));
    });
    return true;
  }
  if (fix.kind === "clearUndo") return written(model, step, withoutUndo(step));
  return (
    fix.kind === "waitForCall" &&
    written(model, step, { ...structuredClone(step), mode: "wait" })
  );
}

/** A step without the fields that say how it is undone. */
function withoutUndo(step: Step): Record<string, unknown> {
  const next = structuredClone(step) as Record<string, unknown>;
  for (const { path } of clearUndo(step)) delete next[String(path[0])];
  return next;
}
/** Writes a step as it is to be; false when that is how it is. */
function written(
  model: StructuredCanvasAdapter,
  step: Step,
  next: Record<string, unknown>,
): boolean {
  if (JSON.stringify(next) === JSON.stringify(step)) return false;
  model.update(step.id, JSON.stringify(next));
  return true;
}
