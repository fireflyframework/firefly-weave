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
// Runs the fix of a problem from outside step details: the Diagnostics panel
// and the question before publishing. A change is one undo step and says what
// it did, with Undo; the others open step details where the person decides.
import type { DiagnosticLocation } from "../../designer/diagnostic-location";
import type { SuggestedChange } from "../../designer/diagnostics-list";
import type { ToastAction } from "../../toast";
import type { StructuredCanvasAdapter } from "../../model";
import type { Path } from "../ndv/registry";
import type { StepDetailsRequest } from "../ndv/step-details-service";
import { applyTransactionFix, fixedText, type TransactionFix } from "./fixes";

/** What running a fix needs of the editor's shell. */
export interface FixHost {
  readonly model: StructuredCanvasAdapter;
  readonly error: string;
  readonly editorNext?: boolean;
  ensureApplied(): Promise<boolean>;
  perform(edit: () => void): void;
  notify(text: string, action?: ToastAction): void;
  undoIfUnchanged(still: () => boolean): Promise<unknown>;
  applySuggestion(change: SuggestedChange): Promise<void>;
  openStepDetails(
    target: string,
    request?: Partial<StepDetailsRequest>,
  ): Promise<boolean>;
  openDiagnostic(at: DiagnosticLocation): Promise<void>;
  openWorkflow?(uses: string): void;
}

/** Where a fix takes the person, or null for one that only changes the workflow. */
export function fixFocus(
  fix: TransactionFix,
  at: { stepId: string; path: Path },
): {
  stepId: string;
  path: Path;
  open?: "choices" | "document";
} | null {
  switch (fix.kind) {
    case "undo":
      return { stepId: at.stepId, path: ["irreversible"] };
    case "hold":
      return { stepId: fix.stepId, path: fix.path };
    case "suggestion":
      return fix.label === "Set Hold for" ? at : null;
    case "picker":
      return { ...at, open: "choices" };
    case "openAction":
      return { ...at, open: "document" };
    default:
      return null;
  }
}

export async function runProblemFix(
  host: FixHost,
  fix: TransactionFix,
  location: DiagnosticLocation,
): Promise<void> {
  if (fix.kind === "openWorkflow") return host.openWorkflow?.(fix.uses);
  if (fix.kind === "suggestion") {
    await host.applySuggestion(fix.change);
    if (host.error) return;
  } else if (
    fix.kind === "move" ||
    fix.kind === "clearUndo" ||
    fix.kind === "waitForCall"
  )
    return change(host, fix, location.stepId);
  const focus = fixFocus(fix, {
    stepId: location.stepId,
    path: [...location.field, ...location.dataPath],
  });
  if (!focus) return;
  // The classic inspector has no How it's undone: it shows the step.
  if (host.editorNext === false) return host.openDiagnostic(location);
  const { stepId, path, open } = focus;
  await host.openStepDetails(stepId, {
    revealAll: true,
    focus: { kind: "path", path, ...(open ? { open } : {}) },
  });
}

async function change(host: FixHost, fix: TransactionFix, id: string) {
  if (!(await host.ensureApplied())) return;
  const { model } = host;
  const text = fixedText(model.definition, fix, id);
  let changed = false;
  host.perform(() => {
    changed = applyTransactionFix(model, fix, id);
  });
  if (!changed || !text || host.error) return;
  const { opened, revision } = model;
  host.notify(text, {
    label: "Undo",
    run: () =>
      void host.undoIfUnchanged(
        () =>
          host.model === model &&
          model.opened === opened &&
          model.revision === revision,
      ),
  });
}
