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
// trigger or End), whether the workflow settings dialog is open, and the
// dialog that publishes the workflow's own actions before Publish… or
// Simulate goes on. The shell, the canvas and the Add a step panel open
// step details here.
import { Injectable, signal } from "@angular/core";
import type { Path } from "./registry";

export type StepDetailsTab = "parameters" | "settings";
export type Region = "header" | "input" | "parameters" | "output";
export type FocusTarget =
  | { kind: "first" }
  | { kind: "rename" }
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

export type OwnedPublishPurpose = "publish" | "simulate";
/** Publish… or Simulate asks to publish the workflow's own actions first. */
export interface OwnedPublishRequest {
  purpose: OwnedPublishPurpose;
  workflow: { name: string; version: string };
}
export interface OwnedPublishOutcome {
  /** Nothing the workflow owns is left unpublished. */
  done: boolean;
  /** Actions published (or found published) by this dialog. */
  published: number;
  /** Why it stopped, for the banner, when it did. */
  reason?: string;
  /** The workflow source its actions were published for, once all are. */
  source?: string;
}
/** The publish dialog's request, and the command waiting for its outcome. */
export interface OwnedPublishOpen {
  request: OwnedPublishRequest;
  settle(outcome: OwnedPublishOutcome): void;
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
  /**
   * The open "publish this workflow's actions" dialog, if any (opened by
   * `publishOwned` in owned/owned-commands.ts, which loads with Publish…).
   */
  readonly ownedPublish = signal<OwnedPublishOpen | null>(null);
  /** Designers on screen that can show it. */
  mounted = 0;

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
