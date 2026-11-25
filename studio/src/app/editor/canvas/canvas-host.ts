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
// picker, "Move to…", deleting and duplicating, the workflow settings,
// undo and saving.
import type { Signal } from "@angular/core";
import type { Profile, StudioApi } from "../../api";
import type { AnchorRect } from "../../designer/popover-placement";
import type { StructuredCanvasAdapter, Workflow } from "../../model";
import type { ErrorEntry, PinEntry } from "../state/test-data";
import type { CanvasSidecar } from "../state/canvas-sidecar";
import type { ClipboardContent, PastePlan } from "../state/clipboard";
import type { KindContext } from "../ndv/registry";
import type { Insertion } from "./layout-ltr";
import type { CanvasRun } from "./run-state";
import type { StepFacts } from "./tile-facts";

export type WorkflowSection = "spec/inputSchema" | "spec/output";

export interface CanvasHost {
  readonly model: StructuredCanvasAdapter;
  /** Bumped on every workflow change. */
  readonly tick: Signal<number>;
  /** True while a simulation runs: the workflow can't change. */
  readonly editingLocked: boolean;
  /** Where the target's language is read from: its platform, or this computer. */
  readonly api: StudioApi;
  readonly profile: Profile | null;
  /**
   * The Runs tab: the canvas draws runs that happened, read-only, without
   * the draft's handles, editing tools or setup badges.
   */
  readonly viewingRuns?: boolean;
  /** The run drawn on the Runs tab ("run 1c0d7f8a"), once its data has loaded. */
  readonly runData?: { label(): string } | null;
  /** The step "Move to…" is placing, or "". */
  readonly connectingNode: string;
  showTemplates: boolean;
  /** Errors, warnings and setup per step; the same map until they change. */
  canvasFacts(): ReadonlyMap<string, StepFacts>;
  /** The run the canvas draws, or null; the same object until it changes. */
  canvasRun(): CanvasRun | null;
  /** Steps of a draft whose Execute in an environment found no connection for a slot (step ID to slot). */
  readonly realSetup?: Signal<{
    draft: string;
    steps: ReadonlyMap<string, string>;
  } | null>;
  /** The open draft's ID. */
  readonly draftId?: string;
  kindContext(): KindContext;
  label(kind: string): string;
  /** Selects a step; `open` also shows it in the inspector. */
  selectStep(id: string, open: boolean): Promise<void>;
  /** Clears the editor's selection; false when unapplied edits keep it. */
  deselect(): Promise<boolean>;
  /** Shows the workflow settings at their Inputs or Result section. */
  openWorkflowSection(section: WorkflowSection): Promise<void>;
  /** Opens the workflow a call step calls (`name@version`). */
  openWorkflow?(uses: string): void;
  /** Opens a trigger choice's settings (webhook, schedule, Kafka, email, app event). */
  openTrigger(id: string): Promise<void>;
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
  /** A step kind is being dragged from the editor, to drop on a "+". */
  readonly dragPreview: unknown;
  /** "Move to…": the step waits for a "+". */
  startMove(id: string): void;
  /** Ends "Move to…" and any drag the editor tracks. */
  cancelGesture(): void;
  /** Cancels a move and returns focus to its step. */
  cancelMove(): void;
  /** Selects a step and moves focus into its details: the title, or its name to rename it. */
  openStep(id: string, focus: "details" | "rename" | "issues"): Promise<void>;
  /** Deletes steps, each with everything inside it, as one undo step with an Undo toast. */
  removeSteps(ids: readonly string[]): Promise<void>;
  /** Copies a step, or a run of steps, right after itself. */
  duplicateSteps(ids: readonly string[]): Promise<void>;
  /** Settles unapplied edits (the inspector, Source); false when the person kept them. */
  ensureApplied(): Promise<boolean>;
  /** Runs a model edit and records it; `error` then says why it failed, or is "". */
  perform(edit: () => void): void;
  readonly error: string;
  /** Undoes the last step while `still()` holds; says how it went. */
  undoIfUnchanged(
    still: () => boolean,
  ): Promise<"undone" | "changed" | "kept" | "locked">;
  /** Adds a decision with one path per answer after a human task. */
  branchOnDecision(id: string): Promise<void>;
  /** A dragged step kind may drop here. */
  allowDrop(event: DragEvent): void;
  /** Inserts the step kind being dropped on a "+". */
  dropStep(event: DragEvent, insert: Insertion): Promise<void>;
  /** Shows an outcome in a toast, which also announces it; with an action such as Undo when given. */
  notify(text: string, action?: { label: string; run: () => void }): void;
  /** Applies a canvas sidecar change (and a workflow change it needs) as one undo step; false when refused. */
  changeCanvas(next: CanvasSidecar, options?: { workflow?: Workflow }): boolean;
  /** Removes a trigger choice, asking first for Called by a workflow, with an Undo toast. */
  removeTrigger(id: string): Promise<void>;
  undo(): void;
  redo(): void;
  /** Ctrl/Cmd+S: save the draft when connected, or save to a file locally. */
  saveShortcut(): void;
  /**
   * Pastes steps read from the clipboard at a place, asking first when they
   * bring connection slots or AI profiles, as one undo step; the plan it
   * applied, or null when nothing was pasted (it said why).
   */
  pasteSteps(
    content: ClipboardContent,
    at: Insertion,
  ): Promise<PastePlan | null>;
  /**
   * Each pinned step's stale words (null when current); a step that isn't
   * here has no pin. The same map until test data or the workflow changes.
   */
  pinStates(): ReadonlyMap<string, { stale: string | null }>;
  /** The workflow's pinned output, for copying with the steps (run-recorded pins stay behind). */
  pinsForCopy(): Readonly<Record<string, PinEntry>>;
  /** The workflow's simulated failures, for copying with the steps (run-recorded ones stay behind). */
  failuresForCopy(): Readonly<Record<string, ErrorEntry>>;
  /** P and the menu, for pinnable steps: unpins the pinned ones as one Undo; others say how to pin. */
  togglePins(ids: readonly string[]): void;
  /** Execute workflow (simulated, on the Studio host); absent where Execute isn't offered. */
  executeWorkflow?(): void;
  /** The line under a step that has data ("3 fields", "Path: amount > 1000"), or null. */
  summaryOf?(stepId: string): string | null;
  /** Why the simulated run stopped at a step ("Step check needs test data."), or null. */
  runNote?(stepId: string): string | null;
  /** The code and message of the step a simulated run failed at, or null. */
  runFailure?(stepId: string): { code?: string; message?: string } | null;
  /** The last execution reached this step (finished, failed or waiting there); pins don't count. */
  ranInRun?(stepId: string): boolean;
  /** Changes whenever the last results change (the summary lines follow it). */
  runStamp?(): unknown;
  /** L: shows or hides the run log. */
  toggleRunLog?(): void;
  /** F9: Execute workflow pauses before this step (or no longer). */
  toggleBreakpoint?(stepId: string): void;
  /** What the canvas's live region says once a playback ends. */
  runAnnouncement?(): { text: string; urgent: boolean } | null;
  /** Tidy layout: clears legacy positions and moves notes as one undo step, with a toast; false when refused. */
  tidyCanvas(next: CanvasSidecar | null): boolean;
}
