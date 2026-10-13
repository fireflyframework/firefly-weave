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
// Where a "+" of Add a step inserts, and whether that place is still there:
// the steps on either side of it, recorded when the panel opens.
import type { StructuredCanvasAdapter } from "../../model";
import type { Insertion } from "../canvas/layout-ltr";

/** The IDs of the steps just before and just after a place; null at an end. */
export interface PlaceMark {
  before: string | null;
  after: string | null;
}
/**
 * What a place looks like now: the steps on either side of it. Null when its
 * sequence is gone or the index is outside it.
 */
export function placeMark(
  model: StructuredCanvasAdapter,
  at: Insertion,
): PlaceMark | null {
  const list = model.owners().get(at.owner);
  if (!list || at.index < 0 || at.index > list.length) return null;
  return {
    before: list[at.index - 1]?.id ?? null,
    after: list[at.index]?.id ?? null,
  };
}

/**
 * The place is still where it was: its sequence exists, and the same steps
 * are on either side of it. An Undo, Redo or Source edit that moves it, even
 * when the index stays inside the sequence, means it is gone.
 */
export function placeExists(
  model: StructuredCanvasAdapter,
  at: Insertion,
  mark: PlaceMark | null,
): boolean {
  const now = placeMark(model, at);
  return (
    !!now && !!mark && now.before === mark.before && now.after === mark.after
  );
}
