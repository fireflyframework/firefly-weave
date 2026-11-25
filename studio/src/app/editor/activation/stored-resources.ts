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
// What a trigger choice keeps about the platform, per environment ID: the
// resource an activation created or moved for it, and, for a platform event
// trigger, the stops Studio still owes. It is stored with the canvas, so what
// it reads back may have been written by hand: anything malformed counts as
// nothing.
import { isJsonObject } from "../../forms/core/json";
import type { TriggerIntent } from "../state/canvas-sidecar";
import type { PendingStop } from "./event-bindings";

/** The resource ID a trigger choice stored for this environment after creating or moving it. */
export function storedId(
  intent: TriggerIntent,
  environmentId: string,
): string | undefined {
  const resources = intent["resources"];
  const id = isJsonObject(resources) ? resources[environmentId] : undefined;
  return typeof id === "string" ? id : undefined;
}

const isPendingStop = (value: unknown): value is PendingStop =>
  isJsonObject(value) &&
  typeof value["triggerId"] === "string" &&
  typeof value["revision"] === "number" &&
  typeof value["activationId"] === "string";

/** The stops Studio owes in this environment. */
export function pendingStopsOf(
  intent: TriggerIntent,
  environmentId: string,
): PendingStop[] {
  const stops = intent["pendingStops"];
  const here = isJsonObject(stops) ? stops[environmentId] : undefined;
  return Array.isArray(here) ? (here as unknown[]).filter(isPendingStop) : [];
}

/** The trigger choice owing these stops in this environment; the others stay as they were. */
export function withPendingStops(
  intent: TriggerIntent,
  environmentId: string,
  stops: readonly PendingStop[],
): TriggerIntent {
  const kept = intent["pendingStops"];
  const { [environmentId]: had, ...others } = isJsonObject(kept)
    ? (kept as Record<string, unknown>)
    : {};
  if (!stops.length && had === undefined) return intent;
  const { pendingStops: _, ...rest } = intent;
  const next = stops.length
    ? { ...others, [environmentId]: [...stops] }
    : others;
  return (
    Object.keys(next).length ? { ...rest, pendingStops: next } : rest
  ) as TriggerIntent;
}
