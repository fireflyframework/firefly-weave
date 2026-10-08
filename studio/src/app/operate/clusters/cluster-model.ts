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
// What the Clusters tabs say about runners and targets. Pure: the tabs render
// it, tests check it.
import { relativeTime } from "../../format";
import type { Tone } from "../../status-labels";
import type { Adapter, Runner } from "../../operations/deployment-contracts";

/** The icon each adapter shows. */
const icons: Record<Adapter, string> = {
  "docker-compose": "compose",
  kubernetes: "kubernetes",
  "azure-container-apps": "cloud",
};
export function adapterIcon(adapter: Adapter): string {
  return icons[adapter] ?? "clusters";
}

/** Online while the registration's contact has not expired (90 s). */
export type RunnerState = "online" | "offline" | "revoked";
export function runnerState(runner: Runner, now = Date.now()): RunnerState {
  if (runner.revoked) return "revoked";
  return Date.parse(runner.expires_at) > now ? "online" : "offline";
}

/** Health tones: online is ok, offline is down, revoked is off. */
const tones: Record<RunnerState, Tone> = {
  online: "success",
  offline: "danger",
  revoked: "neutral",
};
const labels: Record<RunnerState, string> = {
  online: "Online",
  offline: "Offline",
  revoked: "Revoked",
};
export function runnerTone(runner: Runner, now = Date.now()): Tone {
  return tones[runnerState(runner, now)];
}
export function runnerLabel(runner: Runner, now = Date.now()): string {
  return labels[runnerState(runner, now)];
}

/** The newest registration a target still has, from a fleet-wide list. */
export function targetRunner(
  runners: readonly Runner[],
  targetId: string,
): Runner | null {
  return (
    runners
      .filter((runner) => runner.target_id === targetId && !runner.revoked)
      .sort((a, b) => b.last_seen.localeCompare(a.last_seen))[0] ?? null
  );
}

/** A target card's runner line: "Runner offline · last contact 4 min ago". */
export function runnerLine(runner: Runner | null, now = Date.now()): string {
  if (!runner) return "No runner has registered";
  if (runnerState(runner, now) === "online") return "Runner online";
  const contact = relativeTime(runner.last_seen, new Date(now));
  return contact
    ? `Runner offline · last contact ${contact}`
    : "Runner offline";
}
