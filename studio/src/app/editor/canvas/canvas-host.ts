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
// What the canvas needs from the editor that hosts it. The shell (App)
// implements it with the commands it already has: selecting, the step
// picker, "Move to…", the workflow settings and undo.
import type { Signal } from "@angular/core";
import type { AnchorRect } from "../../designer/popover-placement";
import type { StructuredCanvasAdapter } from "../../model";
import type { KindContext } from "../ndv/registry";
import type { Insertion } from "./layout-ltr";
import type { StepFacts } from "./tile-facts";

export type WorkflowSection = "spec/inputSchema" | "spec/output";

export interface CanvasHost {
  readonly model: StructuredCanvasAdapter;
  /** Bumped on every workflow change. */
  readonly tick: Signal<number>;
  /** True while a simulation runs: the workflow can't change. */
  readonly editingLocked: boolean;
  /** The step whose details hold an edit not applied yet, or "". */
  readonly dirtyStep: string;
  /** The step "Move to…" is placing, or "". */
  readonly connectingNode: string;
  showTemplates: boolean;
  /** Errors, warnings and setup per step; the same map until they change. */
  canvasFacts(): ReadonlyMap<string, StepFacts>;
  kindContext(): KindContext;
  label(kind: string): string;
  /** Selects a step; `open` also shows it in the inspector. */
  selectStep(id: string, open: boolean): Promise<void>;
  /** Shows the workflow settings at their Inputs or Result section. */
  openWorkflowSection(section: WorkflowSection): Promise<void>;
  /** Opens today's step picker for an insertion point, next to an element or a point. */
  openPicker(
    insert: Insertion,
    label: string,
    anchor: HTMLElement | AnchorRect,
  ): void;
  isPickerOpenAt(insert: Insertion): boolean;
  closePicker(): void;
  /** Moves a step there (or places an unplaced one) as one undo step. */
  moveStep(id: string, insert: Insertion): void;
}
