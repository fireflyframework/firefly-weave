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
// How the drawer names the decision recorded on an incident. The incident's
// code says which phase a retry ran again, and whether it was Wait again: a
// transaction's record alone can't. Pure.
import {
  RESOLUTION_LABELS,
  RETRY_LABELS,
} from "../transactions/transaction-record";
import { resolutionLabel, type IncidentView } from "./incidents-model";

const RETRIES: Readonly<Record<string, string>> = {
  "WV-TXN-UNDO-STUCK": RETRY_LABELS.undo,
  "WV-TXN-CANCEL-STUCK": RETRY_LABELS.cancel,
  "WV-TXN-CONFIRM-STUCK": RETRY_LABELS.confirm,
  "WV-TXN-AFTER-PIVOT-STUCK": RETRY_LABELS.step,
  "WV-TXN-SETTLE-TIMEOUT": RESOLUTION_LABELS.wait_again,
};
/** "Retry the undo", "Wait again"…, by the incident's code; null when no retry fits it. */
export function retryLabel(code: string): string | null {
  return RETRIES[code] ?? null;
}

/** How a reconciliation's record says the transaction ended. */
function endedAs(output: unknown): string {
  const as =
    typeof output === "object" && output !== null && "as" in output
      ? output.as
      : null;
  return as === "compensated"
    ? " as undone"
    : as === "completed"
      ? " as finished"
      : "";
}

/**
 * The decision recorded on an incident, in the words it was offered with: a
 * transaction in doubt has its own, every other incident the dialog's.
 */
export function decisionText(
  incident: Pick<IncidentView, "code" | "priority" | "resolution">,
): string {
  const resolution = incident.resolution;
  if (!resolution) return "";
  if (incident.priority !== "high") return resolutionLabel(resolution.kind);
  switch (resolution.kind) {
    case "retry_safe":
      return retryLabel(incident.code) ?? resolutionLabel(resolution.kind);
    case "terminate":
      return RESOLUTION_LABELS.force_end;
    case "accept_reconciled_failure":
      return RESOLUTION_LABELS.it_didnt_happen;
    case "accept_reconciled_result":
      return incident.code === "WV-TXN-PIVOT-UNKNOWN"
        ? RESOLUTION_LABELS.it_happened
        : RESOLUTION_LABELS.mark_reconciled + endedAs(resolution.output);
  }
}
