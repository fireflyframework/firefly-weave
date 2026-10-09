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
import type { ToastAction } from "../../toast";
import { editorNextEnabled } from "../state/editor-flag";

export interface StepDetailsHost {
  readonly model: StructuredCanvasAdapter;
  readonly tick: Signal<number>;
  readonly api: StudioApi;
  readonly profile: Profile | null;
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
  error: string;
  can(capability: string, resource?: string): boolean;
  label(kind: string): string;
  notify(text: string, action?: ToastAction, tone?: "neutral" | "danger"): void;
  /** Runs a model edit: marks the workflow changed, or shows the failure. */
  perform(edit: () => void): void;
  undo(): void;
  redo(): void;
  runCommand(command: "save" | "simulate" | "export"): Promise<unknown>;
  blocker(command: "simulate"): string;
  /** Settles unapplied inspector edits; false when they can't be applied. */
  ensureApplied(): Promise<boolean>;
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
