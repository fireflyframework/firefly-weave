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
export interface AnchorRect {
  left: number;
  top: number;
  width: number;
  height: number;
}
export interface Size {
  width: number;
  height: number;
}
/**
 * Viewport position for a fixed popover. Below the anchor it is pinned by its
 * top edge, above the anchor by its bottom edge, so filtering that shrinks the
 * list keeps it attached to the anchor.
 */
export interface PopoverPlacement {
  left: number;
  top?: number;
  bottom?: number;
  maxHeight: number;
  side: "below" | "above";
}

/**
 * Places a popover next to an anchor inside the viewport. It opens below the
 * anchor unless more room is available above, aligns with the anchor's left
 * edge and is clamped to the viewport margins. All values are viewport pixels.
 */
export function placePopover(
  anchor: AnchorRect,
  size: Size,
  viewport: Size,
  gap = 6,
  margin = 8,
): PopoverPlacement {
  const width = Math.min(size.width, Math.max(0, viewport.width - 2 * margin));
  const below = viewport.height - (anchor.top + anchor.height) - gap - margin;
  const above = anchor.top - gap - margin;
  const side =
    below >= Math.min(size.height, 240) || below >= above ? "below" : "above";
  const left = Math.min(
    Math.max(margin, anchor.left),
    Math.max(margin, viewport.width - width - margin),
  );
  return side === "below"
    ? {
        left,
        top: anchor.top + anchor.height + gap,
        maxHeight: Math.max(0, below),
        side,
      }
    : {
        left,
        bottom: viewport.height - anchor.top + gap,
        maxHeight: Math.max(0, above),
        side,
      };
}
