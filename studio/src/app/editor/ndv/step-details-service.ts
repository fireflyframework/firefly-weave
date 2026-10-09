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
// What step details show now: one open request at a time (a step, the
// trigger or End), and whether the workflow settings dialog is open. The
// shell, the canvas and the Add a step panel open step details here.
import { Injectable, signal } from "@angular/core";
import type { Path } from "./registry";

export type StepDetailsTab = "parameters" | "settings";
export type Region = "header" | "input" | "parameters" | "output";
export type FocusTarget =
  | { kind: "first" }
  | { kind: "firstRequiredEmpty" }
  | { kind: "field"; id: string; tab: StepDetailsTab }
  | { kind: "region"; region: Region }
  /** The field of the step's first issue (a tile badge). */
  | { kind: "firstIssue" }
  /** The field that holds a diagnostic's path (`["with"]`, `["cases", 0, "when"]`). */
  | { kind: "path"; path: Path };
export interface StepDetailsRequest {
  /** A step ID, "$trigger" or "$end". */
  target: string;
  tab: StepDetailsTab;
  focus: FocusTarget;
  /** Inserted a moment ago: required messages wait until a field is left. */
  fresh: boolean;
  /** Opened from an issue: every message shows. */
  revealAll: boolean;
}

export const openRequest = (
  target: string,
  partial: Partial<StepDetailsRequest> = {},
): StepDetailsRequest => ({
  target,
  tab: "parameters",
  focus: { kind: "first" },
  fresh: false,
  revealAll: false,
  ...partial,
});

@Injectable({ providedIn: "root" })
export class StepDetailsService {
  readonly request = signal<StepDetailsRequest | null>(null);
  /** Bumped on every open, so the dialog applies the focus rule again. */
  readonly opening = signal(0);
  readonly workflowSettings = signal(false);

  open(request: StepDetailsRequest): void {
    this.workflowSettings.set(false);
    this.request.set(request);
    this.opening.update((n) => n + 1);
  }
  close(): void {
    this.request.set(null);
  }
  isOpen(): boolean {
    return this.request() !== null || this.workflowSettings();
  }
  openWorkflowSettings(): void {
    this.request.set(null);
    this.workflowSettings.set(true);
  }
  closeWorkflowSettings(): void {
    this.workflowSettings.set(false);
  }
}
