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
import {
  Directive,
  ElementRef,
  afterRenderEffect,
  inject,
  input,
  output,
} from "@angular/core";
import { placePopover } from "../../designer/popover-placement";

/** Scroll only the popup, never the inspector that owns its DOM subtree. */
export function revealPopoverOption(list: HTMLElement, option: HTMLElement) {
  const bounds = list.getBoundingClientRect();
  const row = option.getBoundingClientRect();
  if (row.top < bounds.top) list.scrollTop += row.top - bounds.top;
  else if (row.bottom > bounds.bottom)
    list.scrollTop += row.bottom - bounds.bottom;
}

/** Move by the visible page height, including variable-height option rows. */
export function pageOptionIndex(
  list: HTMLElement,
  index: number,
  direction: 1 | -1,
) {
  const options = [
    ...list.querySelectorAll<HTMLElement>('[role="option"], [role="menuitem"]'),
  ];
  if (!options.length) return -1;
  if (index < 0) return direction === 1 ? 0 : options.length - 1;
  const start = options[index].getBoundingClientRect().top;
  const target = start + direction * list.clientHeight;
  let next = index;
  while (next + direction >= 0 && next + direction < options.length) {
    const candidate = next + direction;
    const top = options[candidate].getBoundingClientRect().top;
    if (direction === 1 ? top > target : top < target) break;
    next = candidate;
  }
  return next === index
    ? Math.max(0, Math.min(options.length - 1, index + direction))
    : next;
}

/** The anchor must intersect its scrolling/clipping parents as well as the window. */
function anchorVisible(anchor: HTMLElement) {
  if (!anchor.isConnected || !anchor.getClientRects().length) return false;
  const rect = anchor.getBoundingClientRect();
  let left = Math.max(0, rect.left);
  let right = Math.min(window.innerWidth, rect.right);
  let top = Math.max(0, rect.top);
  let bottom = Math.min(window.innerHeight, rect.bottom);
  for (
    let parent = anchor.parentElement;
    parent;
    parent = parent.parentElement
  ) {
    const style = getComputedStyle(parent);
    const bounds = parent.getBoundingClientRect();
    if (style.overflowX !== "visible") {
      left = Math.max(left, bounds.left);
      right = Math.min(right, bounds.right);
    }
    if (style.overflowY !== "visible") {
      top = Math.max(top, bounds.top);
      bottom = Math.min(bottom, bounds.bottom);
    }
  }
  return right > left && bottom > top;
}

/** Keeps popovers in the top layer while preserving DOM/ARIA ownership and focus. */
@Directive({
  selector: "[weaveAnchoredPopover]",
  standalone: true,
  host: { popover: "manual" },
})
export class AnchoredPopover {
  open = input(false, { alias: "weaveAnchoredPopover" });
  anchor = input.required<HTMLElement>({ alias: "popoverAnchor" });
  closed = output<void>({ alias: "popoverClosed" });
  private readonly element =
    inject<ElementRef<HTMLElement>>(ElementRef).nativeElement;

  constructor() {
    afterRenderEffect((cleanup) => {
      if (!this.open()) return;
      const anchor = this.anchor();
      const panel = this.element;
      panel.showPopover();
      let frame = 0;
      let previous = "";
      const position = () => {
        cancelAnimationFrame(frame);
        if (!anchorVisible(anchor)) {
          this.closed.emit();
          return;
        }
        const rect = anchor.getBoundingClientRect();
        const viewport = {
          width: window.innerWidth,
          height: window.innerHeight,
        };
        const width = Math.min(Math.max(rect.width, 320), viewport.width - 16);
        panel.style.width = `${width}px`;
        const height = Math.min(panel.scrollHeight + 2, 360);
        const key = `${rect.left},${rect.top},${rect.width},${rect.height},${viewport.width},${viewport.height},${height}`;
        if (key !== previous) {
          previous = key;
          const placement = placePopover(rect, { width, height }, viewport);
          Object.assign(panel.style, {
            position: "fixed",
            boxSizing: "border-box",
            margin: "0",
            right: "auto",
            left: `${placement.left}px`,
            top: placement.top === undefined ? "auto" : `${placement.top}px`,
            bottom:
              placement.bottom === undefined ? "auto" : `${placement.bottom}px`,
            maxHeight: `${Math.min(placement.maxHeight, 360)}px`,
          });
          panel.dataset["side"] = placement.side;
        }
        frame = requestAnimationFrame(position);
      };
      const outside = (event: PointerEvent) => {
        const target = event.target as Node | null;
        if (target && !panel.contains(target) && !anchor.contains(target))
          this.closed.emit();
      };
      position();
      document.addEventListener("pointerdown", outside, true);
      window.addEventListener("resize", position);
      window.addEventListener("scroll", position, true);
      cleanup(() => {
        cancelAnimationFrame(frame);
        document.removeEventListener("pointerdown", outside, true);
        window.removeEventListener("resize", position);
        window.removeEventListener("scroll", position, true);
        if (panel.matches(":popover-open")) panel.hidePopover();
      });
    });
  }
}
