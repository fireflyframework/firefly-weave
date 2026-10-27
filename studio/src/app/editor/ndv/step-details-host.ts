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
// What step details need from the editor that hosts them. The shell (App)
// already has every member; nothing here is new state of its own.
import type { Signal } from "@angular/core";
import type { Profile, StudioApi, Validation } from "../../api";
import type { ContractGap } from "../../designer/contract-gaps";
import type { SuggestedChange } from "../../designer/diagnostics-list";
import type { StructuredCanvasAdapter } from "../../model";
import type { InferSchema } from "../../forms/ui/schema-designer";
import type { AiSheetRequest } from "../../integrations/ai-connection-sheet";
import type { ToastAction } from "../../toast";
import type { StepDetailsRequest } from "./step-details-service";
import { editorNextEnabled } from "../state/editor-flag";
import type { StepFingerprints } from "../state/fingerprints";
import type { TestDataService } from "../state/test-data-service";
import type { ExecuteTarget } from "../state/execution";
import type {
  ExecuteOutcome,
  ExecutionService,
} from "../state/execution-service";

export interface StepDetailsHost {
  readonly model: StructuredCanvasAdapter;
  readonly tick: Signal<number>;
  readonly api: StudioApi;
  readonly profile: Profile | null;
  /** Grows whenever an AI connection or its test changes; AI model lists reload on it. */
  readonly aiSetupGeneration?: number;
  /** Opens the AI connection side sheet (Set up AI). */
  openAiConnectionDialog?(request?: AiSheetRequest): void;
  /** Account/workspace invalidation generation and stable principal identity. */
  readonly stepDataScope?: string;
  /** True while a simulation runs: the workflow can't change. */
  readonly editingLocked: boolean;
  /** "Try the new editor", once the shell keeps it itself. */
  readonly editorNext?: boolean;
  readonly catalogContracts: ReadonlyMap<string, Record<string, unknown>>;
  readonly decisionContracts: ReadonlyMap<string, Record<string, unknown>>;
  readonly actionVersions: Record<string, unknown>[];
  readonly catalogState: "idle" | "loading" | "ready" | "error";
  readonly diagnostics: Validation | null;
  readonly diagnosticsDefinition: unknown;
  readonly contractIssues: readonly (ContractGap & { stepId: string })[];
  readonly workflowSlots: {
    name: string;
    connector: string;
    required: boolean;
  }[];
  readonly windowWidth: number;
  /** Fields from example JSON ("Paste sample JSON"); null hides it. */
  readonly inferSchema: InferSchema | null;
  error: string;
  can(capability: string, resource?: string): boolean;
  label(kind: string): string;
  notify(text: string, action?: ToastAction, tone?: "neutral" | "danger"): void;
  /** Runs a model edit: marks the workflow changed, or shows the failure. */
  perform(edit: () => void): void;
  /** Records what publishing learned (a version is published) in the last undo step, without unsaving the workflow; false when refused. */
  recordOwned(edit: () => void): boolean;
  undo(): void;
  redo(): void;
  /**
   * A toast's Undo: settles text typed in Source, then undoes only while
   * `still` holds; "kept" when the person keeps editing Source, "locked"
   * while a simulation runs.
   */
  undoIfUnchanged(
    still: () => boolean,
  ): Promise<"undone" | "changed" | "kept" | "locked">;
  runCommand(command: "save" | "simulate" | "export"): Promise<unknown>;
  blocker(command: "simulate"): string;
  /** Settles unapplied inspector edits; false when they can't be applied. */
  ensureApplied(): Promise<boolean>;
  /** The open workflow's test data (test input, pinned output), outside undo; null until it loads. */
  readonly testData?: TestDataService | null;
  /** Every step's fingerprints now (memoized on the workflow, the canvas and the test input). */
  /** Null while the shell's copy isn't ready: step details compute their own. */
  testDataPrints?(): ReadonlyMap<string, StepFingerprints> | null;
  /** Opens the test data of the workflow and account shown now; false when it was open already. */
  openTestData?(): boolean;
  /** Executes on the Studio host and keeps the last results; null until it loads. */
  readonly execution?: ExecutionService | null;
  /** Runs one execution (Execute step, Execute previous steps, Execute workflow). */
  executeIn?(target: ExecuteTarget): Promise<ExecuteOutcome>;
  /** This step's pin came from another workflow (pasted, or loaded from a file) and isn't kept yet. */
  pinFromElsewhere?(id: string): boolean;
  /** Saves a platform draft's pending edits now (closing step details); true when nothing is left. */
  flushAutosave?(): Promise<boolean>;
  /**
   * Opens step details on a step, the trigger or End the way every opener
   * does: pending inspector edits are applied first and the step becomes the
   * selection, with the inspector following it. False when it can't open.
   */
  openStepDetails(
    target: string,
    request?: Partial<StepDetailsRequest>,
  ): Promise<boolean>;
  focusStep(id: string): void;
  remove(id?: string): Promise<void>;
  duplicate(id?: string): Promise<void>;
  branchOnDecision(id?: string): Promise<void>;
  applySuggestion(change: SuggestedChange): Promise<void>;
  cacheContract(uses: string, document: Record<string, unknown> | null): void;
  cacheDecisionContract(uses: string, document: Record<string, unknown>): void;
  loadActionCatalog(append?: boolean, force?: boolean): Promise<void>;
  openConnectionDialog(slot?: string): void;
  refreshView(): void;
}

/** Step details follow "Try the new editor": the shell's own value once it keeps one. */
export const stepDetailsEnabled = (host: { editorNext?: boolean }): boolean =>
  host.editorNext ?? editorNextEnabled();
