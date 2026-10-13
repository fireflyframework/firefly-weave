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
// What the Add a step panel needs from the editor that hosts it. The shell
// (App) implements it; the canvas opens the panel through
// CanvasHost.openPicker, which records an AddStepRequest.
import type { Profile, StudioApi } from "../../api";
import type { AnchorRect } from "../../designer/popover-placement";
import type { Kind, StructuredCanvasAdapter } from "../../model";
import type { Insertion } from "../canvas/layout-ltr";
import type { EarlyKey } from "./early-keys";
import type { PanelEntry } from "./entries";
import type { InsertOutcome, InsertRequest } from "./insert";

export interface AddStepRequest {
  /** Where a step goes. */
  insert: Insertion;
  /** What the "+" that opened it is called: "Add a step after check". */
  label: string;
  /** Focus returns here when the panel closes without inserting. */
  anchor: HTMLElement | AnchorRect;
  /** The view it opens on. */
  start: "triggers" | "next";
  /** StructuredCanvasAdapter.opened when it opened: another workflow closes it. */
  opened: number;
}
export interface AddStepHost {
  readonly addStep: AddStepRequest | null;
  readonly model: StructuredCanvasAdapter;
  readonly editingLocked: boolean;
  readonly api: StudioApi;
  readonly profile: Profile | null;
  /** The signed-in account; null while it is being checked. */
  readonly identity: unknown;
  can(capability: string, resource?: string): boolean;
  readonly actionVersions: Record<string, unknown>[];
  readonly catalogContracts: ReadonlyMap<string, Record<string, unknown>>;
  readonly catalogState: "idle" | "loading" | "ready" | "error";
  readonly actionNextCursor: string | null;
  closePicker(): void;
  /** Loads published actions' contracts: they say which app each action uses. */
  warmActionContracts(): void;
  /** Keys pressed since the panel was asked to open, oldest first; stops holding them. */
  takeEarlyKeys(): readonly EarlyKey[];
  notify(text: string): void;
  undo(): void;
  redo(): void;
  /** Inserts what the panel chose, at its place, as one undo step. */
  insertFromPanel(
    entry: PanelEntry,
    request: InsertRequest,
  ): Promise<InsertOutcome>;
  /** Starts dragging a step kind: the canvas accepts it on a "+". */
  dragPalette(event: DragEvent, kind: Kind): void;
  dragPreview: unknown;
}
