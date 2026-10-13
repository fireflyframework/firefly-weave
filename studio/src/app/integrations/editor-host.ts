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
// What the lazily loaded integration UI may read and change in the editor
// shell. Types only: the shell (App) passes itself, so the integration logic
// stays out of the initial bundle while the shell keeps owning its state.
import type { Profile, StudioApi } from "../api";
import type { Identity } from "../connection";
import type { Node, StructuredCanvasAdapter } from "../model";
import type {
  BuilderTab,
  HttpActionUse,
  UseContext,
} from "./http-action-builder";
import type { WorkflowSlot } from "./slot-binding";

export interface EditorHost {
  readonly api: StudioApi;
  readonly model: StructuredCanvasAdapter;
  readonly profile: Profile | null;
  readonly identity: Identity | null;
  readonly view: string;
  can(capability: string, resource?: string): boolean;
  // The published action catalog the shell loads once per workspace.
  readonly actionVersions: Record<string, unknown>[];
  readonly actionNextCursor: string | null;
  readonly catalogState: "idle" | "loading" | "ready" | "error";
  readonly catalogError: { message: string; code: string } | null;
  readonly catalogAppending: boolean;
  /** Action contracts by name@version (a new map whenever one changes). */
  readonly catalogContracts: ReadonlyMap<string, Record<string, unknown>>;
  loadActionCatalog(append?: boolean, force?: boolean): Promise<void>;
  /** Caches an action contract, or drops it when `document` is null. */
  cacheContract(uses: string, document: Record<string, unknown> | null): void;
  // The workflow being edited.
  readonly selected: Node | undefined;
  readonly workflowSlots: WorkflowSlot[];
  /** Runs a model edit; failures land in `error`. */
  perform(edit: () => void): void;
  /** Settles unapplied inspector edits; false when the command must stop. */
  ensureApplied(): Promise<boolean>;
  focusStep(id: string): void;
  focusInspector(): void;
  newWorkflow(): void;
  scheduleFit(): void;
  /** Asks Angular to render after asynchronous work. */
  refreshView(): void;
  error: string;
  message: string;
  dirty: boolean;
  showInspector: boolean;
  showPalette: boolean;
  // The integration dialogs the shell opens.
  apiBuilder: { context: UseContext; tab: BuilderTab; step: string } | null;
  lastUse: HttpActionUse | null;
  connectionDialog: {
    fromBuilder: HttpActionUse["connection"] | null;
    /** Opened from a workflow: "Back to the workflow" returns there. */
    fromWorkflow?: boolean;
  } | null;
  /** "Payments / Production" for the selected workspace. */
  readonly workspaceText: string;
  showTemplates: boolean;
  openApiBuilder(context: UseContext, tab?: BuilderTab): Promise<void>;
  openConnectionDialog(slot?: string): void;
  refresh(append?: boolean): Promise<void>;
}
