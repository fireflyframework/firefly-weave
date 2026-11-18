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
// A loop on the canvas is a frame around its body. This module holds what
// the frame says and its sizes; the layout places frames, the canvas draws
// them. Pure, and only loaded with the editor's canvas.
import type { Step } from "../../model";
import { printFormula } from "../formula/printer";

/** Frame sizes in canvas units. */
export const FRAME = {
  /** The header band: the loop's icon, name and list. */
  header: 32,
  /** Under the header, above the body's row. */
  top: 24,
  /**
   * Left and right of the body: room for the inside half of the entry edge
   * (where "Insert a step at the start" is hovered) and for the "+" after
   * the last step, 40 units out.
   */
  side: 64,
  /** Under a step's tile: its label gap and three label lines at 60%. */
  labels: 88,
  /** Under the labels of the body's lowest step, or a nested frame's bottom. */
  bottom: 24,
  /** The narrowest frame, so a just-added loop's header fits whole at 60%. */
  minWidth: 360,
  /** Under a frame: its own label line (the loop's result once it ran) and a gap. */
  after: 48,
} as const;

/** "For each item in input.orders · up to 4 at a time"; "…" until the list is chosen. */
export function loopHeader(
  step: Step,
  print: (expression: unknown) => string | null = printFormula,
): string {
  const items =
    step["items"] === undefined ? null : print(step["items"]) || null;
  const concurrency = step["concurrency"];
  const atATime =
    typeof concurrency === "number" && concurrency > 1
      ? ` · up to ${concurrency} at a time`
      : "";
  return `For each item in ${items ?? "…"}${atATime}`;
}
