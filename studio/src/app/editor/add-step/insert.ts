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
// Turns a chosen Add a step entry into an edit: a step at the place the "+"
// stood for, a trigger choice, a common step (Call an API, email, files) or
// a published action. Each is one undo step, and the shell opens what was
// added. A place that is gone, or a workflow that changed, refuses instead
// of inserting elsewhere.
import { kinds, type Kind, type StructuredCanvasAdapter } from "../../model";
import type { Insertion, TriggerKind } from "../canvas/layout-ltr";
import { uniqueIdentifier } from "../ndv/params/identifiers";
import { addTrigger, type TriggerChange } from "../state/canvas-sidecar";
import type { PanelEntry, Preset } from "./entries";
import { placeExists, type PlaceMark } from "./place";

export { placeExists, placeMark, type PlaceMark } from "./place";

/** What inserting needs from the editor; the shell (App) implements it. */
export interface InsertHost {
  readonly model: StructuredCanvasAdapter;
  readonly editingLocked: boolean;
  /** Settles unapplied edits in the inspector; false when the person kept them. */
  ensureApplied(): Promise<boolean>;
  /** Runs a model edit and records it; the error sentence, or "" when it worked. */
  edit(change: () => void): string;
  notify(text: string): void;
  /** After a step was inserted: its step details, on the first required empty field. */
  openInserted(id: string): void;
  /** After a trigger was chosen: its details (the input form), or its tile. */
  openTrigger(id: string, kind: TriggerKind): void;
  /**
   * Call an API, email and file steps; returns the new step's ID, or "" when
   * nothing was inserted. Inserts only while `still()` holds.
   */
  insertCommon(
    common: string,
    at: Insertion,
    connector: string | undefined,
    still: () => boolean,
  ): Promise<string>;
  /** A published action with its connection slot, while `still()` holds; the new step's ID, or "". */
  insertPublished(
    uses: string,
    id: string | undefined,
    at: Insertion,
    still: () => boolean,
  ): Promise<string>;
  openTemplates(): void;
  openNewApiAction(): void;
  loadActionCatalog(append?: boolean, force?: boolean): Promise<void>;
}
export interface InsertRequest {
  at: Insertion;
  /** StructuredCanvasAdapter.opened when the panel opened. */
  opened: number;
  /** The steps on either side of the place when the panel opened (placeMark). */
  mark: PlaceMark | null;
}
export type InsertOutcome = "done" | "kept-open" | "refused" | "gone";

export const GONE = "That place in the workflow changed. Choose + again.";
export const PRESET_FIELDS: Readonly<Record<Preset, Record<string, unknown>>> =
  {
    "text-template": {
      value: { op: { name: "concat", args: [{ literal: "" }] } },
    },
    "approve-reject": {},
    "free-text": {
      title: { literal: "Answer a question" },
      formSchema: {
        type: "object",
        required: ["answer"],
        properties: { answer: { type: "string", title: "Answer" } },
      },
      decisions: ["submit"],
    },
    "custom-form": {
      title: { literal: "Fill in a form" },
      formSchema: { type: "object", properties: {} },
      decisions: ["submit"],
    },
  };
/** Readable IDs of preset steps, numbered like step details' own steps (`-2`, `-3`). */
export const PRESET_IDS: Readonly<Partial<Record<Preset, string>>> = {
  "text-template": "text-template",
  "free-text": "ask-person",
  "custom-form": "form",
};

export async function insertEntry(
  host: InsertHost,
  entry: PanelEntry,
  request: InsertRequest,
): Promise<InsertOutcome> {
  const insert = entry.insert;
  if (!insert || entry.disabled) return "refused";
  if (insert.kind === "command") {
    switch (insert.command) {
      case "templates":
        host.openTemplates();
        return "done";
      case "new-api-action":
        host.openNewApiAction();
        return "done";
      case "load-more":
        await host.loadActionCatalog(true);
        return "kept-open";
      case "retry-catalog":
        await host.loadActionCatalog(false, true);
        return "kept-open";
    }
  }
  const model = host.model;
  if (host.editingLocked || model.readonly) {
    host.notify(
      model.readonly
        ? "Fix the source before adding steps."
        : "Editing is paused while the simulation runs.",
    );
    return "refused";
  }
  const moved = () =>
    model.opened !== request.opened ||
    !placeExists(model, request.at, request.mark);
  if (moved()) {
    host.notify(GONE);
    return "gone";
  }
  if (insert.kind === "trigger") {
    let change: TriggerChange;
    try {
      change = addTrigger(
        model.canvas,
        model.definition,
        insert.trigger,
        model.stepIdSet().size > 0,
      );
    } catch (error) {
      host.notify(error instanceof Error ? error.message : String(error));
      return "refused";
    }
    const failed = host.edit(() =>
      model.batch(() => {
        if (change.workflow) model.updateWorkflow(change.workflow);
        model.canvas = change.canvas;
      }),
    );
    if (failed) return "refused";
    host.openTrigger(change.id, insert.trigger);
    return "done";
  }
  if (!(await host.ensureApplied())) return "refused";
  if (moved()) {
    host.notify(GONE);
    return "gone";
  }
  if (insert.kind === "action" || insert.kind === "common") {
    // These wait for code or an action's contract: the host inserts only
    // while the place is still there.
    const still = () => !moved();
    const id =
      insert.kind === "action"
        ? await host.insertPublished(insert.uses, insert.id, request.at, still)
        : await host.insertCommon(
            insert.common,
            request.at,
            insert.connector,
            still,
          );
    if (!id) {
      if (!moved()) return "refused";
      host.notify(GONE);
      return "gone";
    }
    host.openInserted(id);
    return "done";
  }
  if (!(kinds as readonly string[]).includes(insert.step)) {
    host.notify("Studio can't add this step yet.");
    return "refused";
  }
  const preset = insert.preset;
  const named = preset ? PRESET_IDS[preset] : undefined;
  const failed = host.edit(() =>
    model.insert(insert.step as Kind, request.at.owner, request.at.index, {
      ...(preset ? structuredClone(PRESET_FIELDS[preset]) : {}),
      ...(named ? { id: uniqueIdentifier(named, model.stepIdSet()) } : {}),
    }),
  );
  if (failed) return "refused";
  host.openInserted(model.selected);
  return "done";
}
