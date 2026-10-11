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
// The part of a scrolling pane the person can see, and moves that keep a
// popup or a field's top inside it.

/** The nearest ancestor that scrolls vertically. */
export function scrollingParent(element: HTMLElement): HTMLElement | null {
  for (
    let parent = element.parentElement;
    parent;
    parent = parent.parentElement
  )
    if (/^(auto|scroll)$/.test(getComputedStyle(parent).overflowY))
      return parent;
  return null;
}

/** What the person sees of a scroller: below the sticky bars that cover its top. */
export function visibleBox(scroller: HTMLElement) {
  const bounds = scroller.getBoundingClientRect();
  let top = bounds.top;
  for (const child of scroller.querySelectorAll<HTMLElement>(
    ":scope > *, :scope > * > *",
  ))
    if (getComputedStyle(child).position === "sticky")
      top = Math.max(top, child.getBoundingClientRect().bottom);
  return {
    top,
    bottom: bounds.top + scroller.clientHeight,
    right: bounds.left + scroller.clientWidth,
  };
}

/** Scrolls as little as needed for the element's top to be in view. */
export function revealTop(element: HTMLElement) {
  const scroller = scrollingParent(element);
  if (!scroller) return;
  const view = visibleBox(scroller);
  const top = element.getBoundingClientRect().top;
  if (top < view.top || top >= view.bottom)
    scroller.scrollTop += top - view.top;
}
