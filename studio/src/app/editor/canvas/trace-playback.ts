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
// A simulated run played back on the canvas, one step every 120 ms: the
// current tile gets the live border, finished ones the check, a failed one
// the error. With reduced motion only the last frame is shown. DOM-free.
import type { ExecuteResponse } from "../state/execution";
import type { CanvasRun } from "./run-state";

export const PLAYBACK_STEP_MS = 120;

/** The run as it stands after the whole trace. */
export function finalRun(response: ExecuteResponse): CanvasRun {
  const top = response.trace.filter((entry) => !entry.frame);
  const done = top.filter((e) => e.status === "completed").map((e) => e.nodeId);
  const failed = top.filter((e) => e.status === "failed").map((e) => e.nodeId);
  const waiting = top
    .filter((e) => e.status === "waiting")
    .map((e) => e.nodeId);
  const blocked = response.blocked ? [response.blocked.nodeId] : [];
  const status =
    response.status === "completed"
      ? "succeeded"
      : response.status === "blocked"
        ? "waiting"
        : "failed";
  return {
    mode: "simulated",
    status,
    current: [...new Set([...failed, ...blocked])],
    active: waiting.filter((id) => !blocked.includes(id)),
    done: [...new Set(done)],
  };
}

/**
 * The frames of the playback: one per trace entry (that step live, the ones
 * before it finished), then the final run. Reduced motion: the final run only.
 */
export function playbackFrames(
  response: ExecuteResponse,
  reducedMotion: boolean,
): CanvasRun[] {
  const last = finalRun(response);
  if (reducedMotion) return [last];
  const top = response.trace.filter((entry) => !entry.frame);
  const frames: CanvasRun[] = top.map((entry, index) => ({
    mode: "simulated",
    status: "running",
    current: [entry.nodeId],
    active: [],
    done: [
      ...new Set(
        top
          .slice(0, index)
          .filter((e) => e.status === "completed")
          .map((e) => e.nodeId),
      ),
    ],
  }));
  return [...frames, last];
}

/** What the polite live region says when the playback ends. */
export function finalAnnouncement(response: ExecuteResponse): string {
  if (response.status === "completed") {
    const count = new Set(
      response.trace
        .filter((e) => !e.frame && e.status === "completed")
        .map((e) => e.nodeId),
    ).size;
    return `Executed ${count} step${count === 1 ? "" : "s"}.`;
  }
  if (response.status === "blocked" && response.blocked)
    return response.blocked.message;
  if (response.status === "limit")
    return "The simulation stopped at its limit.";
  return "The test run failed.";
}
