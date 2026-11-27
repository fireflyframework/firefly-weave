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
// What a run's transaction adds to a tile: its state and its corner badge.
// Pure, and only loaded with the editor's canvas.
import type { FrameState, StepState } from "../transactions/rollback";
import type { TileRun } from "./run-state";
import {
  failureCodeText,
  tileBadge,
  type TileBadge,
  type TileFacts,
} from "./tile-facts";

/**
 * A step's run as its transaction tells it: live while it is undone, failed
 * when it failed or can't be undone; else as the run says. A failure its own
 * On error handled stays so.
 */
export function transactionTileRun(
  run: TileRun,
  state: Pick<StepState | FrameState, "tone"> | null | undefined,
): TileRun {
  if (state?.tone === "live") return run === "waiting" ? run : "live";
  return state?.tone === "danger" && run !== "handled" ? "failed" : run;
}

/** A step's corner badge in a run, with where its transaction left it. */
export function stepBadge(
  facts: TileFacts,
  left: StepState | null | undefined,
): TileBadge | null {
  const badge = tileBadge(facts);
  if (!left || facts.run === "live" || facts.run === "waiting") return badge;
  const { status, text, tone, undone } = left;
  // The step its transaction was undone for: its own code, not the run's.
  if (facts.run === "failed" && undone) {
    const code = facts.failure?.code;
    return {
      kind: "failed",
      tone: "danger",
      icons: ["critical"],
      text: `${code ? `Failed: ${failureCodeText(code)}` : "Failed"}. The transaction was undone.`,
    };
  }
  switch (status) {
    case "undone":
    case "cancelled":
      return { kind: "undone", tone: "warning", icons: ["undo"], text };
    case "undo_stuck":
    case "cancel_stuck":
    case "confirm_stuck":
      return { kind: "stuck", tone: "danger", icons: ["critical"], text };
    case "confirmed":
      return badge?.kind === "done" ? { ...badge, text } : badge;
    case "done":
      // Still applied in a transaction left in doubt.
      return tone === "warning"
        ? {
            kind: "in-doubt",
            tone,
            icons: [text === "Still reserved" ? "lock" : "warning"],
            text,
          }
        : badge;
    default:
      return badge;
  }
}
