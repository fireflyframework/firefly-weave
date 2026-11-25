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
// Whether a trigger choice was activated with the settings it has now: the
// key of its settings, without what Studio stores about its resources, is
// kept per environment when an activation sets the trigger up. Kept apart
// from the step details targets, which the first page loads.
import { canonicalJson } from "../../forms/core/json";
import type { TriggerIntent } from "../state/canvas-sidecar";

/** A trigger choice's settings, without what Studio stores about its resources. */
export function settingsKey(intent: TriggerIntent): string {
  const {
    id: _id,
    resources: _resources,
    activated: _activated,
    pendingStops: _pendingStops,
    ...settings
  } = intent;
  return canonicalJson(settings);
}

/** Whether a trigger choice was activated somewhere with the settings it has now. */
export function activatedAsIs(intent: TriggerIntent): boolean {
  const activated = intent["activated"];
  if (!activated || typeof activated !== "object") return false;
  const key = settingsKey(intent);
  return Object.values(activated).some((value) => value === key);
}
