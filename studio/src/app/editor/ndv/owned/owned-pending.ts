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
// A platform check of a workflow whose Call an API, email or file steps call
// actions that aren't published yet: the check can't find those actions, and
// that is expected, because Publish… and Simulate publish them first. Each
// such "unknown action" becomes a note at its step, counted in
// `ownedPending`, so the publish gate can go on. Every other problem stays,
// including a published owned action the catalog lacks.
import { getAt, parsePointer } from "../../../forms/core/json";
import type { CanvasSidecar } from "../../state/canvas-sidecar";
import { isFrozen } from "./owned-actions";
import { ownedActionsOf } from "./owned-store";

export const OWNED_UNPUBLISHED = "WV-STUDIO-OWNED_UNPUBLISHED";
interface CheckDiagnostic {
  message: string;
  code?: string;
  severity?: string;
  [key: string]: unknown;
}
export interface PendingAware {
  validationOk?: boolean;
  errorCount: number;
  diagnostics: CheckDiagnostic[];
  ownedPending?: number;
}

export function withOwnedPending<T extends PendingAware>(
  result: T,
  definition: unknown,
  canvas: CanvasSidecar,
): T {
  const pending = new Set(
    Object.entries(ownedActionsOf(canvas))
      .filter(([, recipe]) => !isFrozen(recipe))
      .map(([uses]) => uses),
  );
  if (!pending.size || !Array.isArray(result.diagnostics)) return result;
  let notes = 0;
  const diagnostics = result.diagnostics.map((diagnostic) => {
    if (
      diagnostic.code !== "WV-COMP-UNKNOWN_ACTION" ||
      diagnostic.severity === "info"
    )
      return diagnostic;
    const at = parsePointer(String(diagnostic["path"] ?? ""));
    const uses = at ? getAt(definition, at) : undefined;
    if (typeof uses !== "string" || !pending.has(uses)) return diagnostic;
    notes++;
    return {
      ...diagnostic,
      code: OWNED_UNPUBLISHED,
      severity: "info",
      message:
        "This step's action is published with the workflow: Publish… and Simulate publish it first.",
    };
  });
  if (!notes) return result;
  const errorCount = Math.max(0, result.errorCount - notes);
  return {
    ...result,
    diagnostics,
    errorCount,
    // The platform's check passes exactly when it counts no error.
    ...(errorCount === 0 && result.validationOk === false
      ? { validationOk: true }
      : {}),
    ownedPending: notes,
  };
}
