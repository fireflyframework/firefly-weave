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
// The keys of undo steps in test data: a step's confirm, release or undo is
// the step's own key with a segment (`hold#confirm`, `add[2]#undo`), as the
// platform writes them (PHASE_SEGMENTS in contracts/transactions.py).
import { instanceStep } from "../state/instance-keys";

/** A step's confirm, its release or its undo, by its segment in a key. */
export type Phase = "confirm" | "cancel" | "undo";

/** The undo step a key names and the step instance it settles; null for any other key. */
export function splitPhase(key: string): { key: string; phase: Phase } | null {
  const named = /^([^#~]+)#(confirm|cancel|undo)$/.exec(key);
  return named && { key: named[1], phase: named[2] as Phase };
}
/** The key of an undo step of a step instance: `add[2]#undo`. */
export const phaseKey = (key: string, phase: Phase): string =>
  `${key}#${phase}`;
/** The key that stands for every item: `send[2]` gives `send`, `add[2]#undo` gives `add#undo`. */
export function everyItem(key: string): string {
  const named = splitPhase(key);
  return named
    ? phaseKey(instanceStep(named.key), named.phase)
    : instanceStep(key);
}
