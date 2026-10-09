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
// Drag transport stays transient; the target session remains the write authority.
import {
  DestroyRef,
  Directive,
  ElementRef,
  effect,
  inject,
  input,
  signal,
} from "@angular/core";
import {
  autoScrollDelta,
  caretAt,
  caretStops,
  parseDrag,
  stickyTarget,
  type DragRef,
} from "./drop";
import type { FormSession } from "./form-session";

export const REF_MIME = "application/x-weave-ref";
export const activeDrag = signal<DragRef | null>(null);
export const FIELD_DROP = "weavefielddrop";
export const FIELD_DRAG_START = "weavefielddragstart";
export const FIELD_MAPPING_CHECK = "weavefieldmappingcheck";
export interface FieldDrop {
  drag: DragRef;
  x: number;
  y: number;
  shift: boolean;
}

/** Ask the affected mounted controls before a write could replace their local drafts. */
export function canMapFields(root: HTMLElement, session: FormSession): boolean {
  const fields = [
    ...(root.matches("weave-param-field") ? [root] : []),
    ...root.querySelectorAll<HTMLElement>("weave-param-field"),
  ];
  if (
    fields.some(
      (field) =>
        !field.dispatchEvent(
          new Event(FIELD_MAPPING_CHECK, { cancelable: true }),
        ),
    )
  ) {
    session.announce(
      "Finish or clear the unapplied value before mapping data.",
    );
    return false;
  }
  return true;
}

/** The caret under a drop, including control scrolling and wrapped textarea lines. */
export function caretFromPoint(
  control: Element | null,
  clientX: number,
  clientY?: number,
): number | null {
  if (
    !(
      control instanceof HTMLInputElement ||
      control instanceof HTMLTextAreaElement
    ) ||
    !Number.isFinite(clientX)
  )
    return null;
  const style = getComputedStyle(control);
  const box = control.getBoundingClientRect();
  if (
    clientX < box.left ||
    clientX > box.right ||
    (clientY !== undefined && (clientY < box.top || clientY > box.bottom))
  )
    return null;
  if (control instanceof HTMLInputElement) {
    const context = document.createElement("canvas").getContext("2d");
    if (!context) return null;
    context.font = style.font;
    const x =
      clientX -
      box.left -
      parseFloat(style.borderLeftWidth) -
      parseFloat(style.paddingLeft) +
      control.scrollLeft;
    return caretAt(control.value, x, (text) => context.measureText(text).width);
  }
  if (clientY === undefined || !Number.isFinite(clientY)) return null;
  // A layout mirror provides real wrapped glyph geometry without moving focus or selection.
  const mirror = document.createElement("div");
  for (const property of [
    "font",
    "line-height",
    "letter-spacing",
    "padding",
    "border-width",
    "box-sizing",
    "word-break",
    "overflow-wrap",
    "tab-size",
    "text-indent",
    "text-align",
    "direction",
  ])
    mirror.style.setProperty(property, style.getPropertyValue(property));
  Object.assign(mirror.style, {
    position: "fixed",
    visibility: "hidden",
    pointerEvents: "none",
    borderStyle: "solid",
    left: `${box.left - control.scrollLeft}px`,
    top: `${box.top - control.scrollTop}px`,
    width: `${box.width}px`,
    whiteSpace: control.wrap === "off" ? "pre" : "pre-wrap",
    overflowWrap: "break-word",
  });
  const text = document.createTextNode(control.value + "\u200b");
  const stops = caretStops(control.value);
  mirror.append(text);
  document.body.append(mirror);
  try {
    const range = document.createRange();
    const rect = (index: number) => {
      range.setStart(text, stops[index]);
      range.setEnd(text, stops[index + 1] ?? control.value.length + 1);
      return range.getBoundingClientRect();
    };
    const lower = (
      predicate: (index: number) => boolean,
      start = 0,
      end = stops.length - 1,
    ) => {
      while (start < end) {
        const middle = Math.floor((start + end) / 2);
        if (predicate(middle)) end = middle;
        else start = middle + 1;
      }
      return start;
    };
    const first = lower((index) => rect(index).bottom > clientY);
    const line = rect(first);
    const end = lower((index) => rect(index).top >= line.bottom, first);
    return stops[
      lower(
        (index) => {
          const glyph = rect(index);
          return clientX < glyph.left + glyph.width / 2;
        },
        first,
        end,
      )
    ];
  } finally {
    mirror.remove();
  }
}

@Directive({
  selector: "[weaveDragMap]",
  standalone: true,
  host: {
    "(document:dragstart)": "begin($event)",
    "(dragover)": "over($event)",
    "(dragleave)": "leave($event)",
    "(drop)": "drop($event)",
    "(document:dragend)": "end()",
    "(document:drop)": "end()",
    "(document:keydown.escape)": "cancel($event)",
  },
})
export class DragMap {
  session = input.required<FormSession>({ alias: "weaveDragMap" });
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private hovered: HTMLElement | null = null;
  private owner: FormSession | null = null;
  constructor() {
    effect(() => {
      this.session();
      this.end();
    });
    inject(DestroyRef).onDestroy(() => this.end());
  }
  private accepts(event: DragEvent): boolean {
    return !!event.dataTransfer?.types.includes(REF_MIME);
  }
  begin(event: DragEvent) {
    if (!this.accepts(event)) {
      this.end();
      return;
    }
    this.owner = this.session();
    for (const field of this.element.nativeElement.querySelectorAll(
      "weave-param-field",
    ))
      field.dispatchEvent(new Event(FIELD_DRAG_START));
  }
  private target(x: number, y: number) {
    const root = this.element.nativeElement;
    const box = root.getBoundingClientRect();
    if (x < box.left || x > box.right || y < box.top || y > box.bottom)
      return null;
    return stickyTarget(
      [...root.querySelectorAll<HTMLElement>("weave-param-field")]
        .filter((item) => item.getClientRects().length > 0)
        .map((item) => ({ item, box: item.getBoundingClientRect() })),
      x,
      y,
    );
  }
  over(event: DragEvent) {
    if (!this.accepts(event)) return;
    if (!this.owner) this.begin(event);
    if (this.owner !== this.session() || !this.owner?.isCurrent()) {
      this.end();
      return;
    }
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
    this.hover(this.target(event.clientX, event.clientY));
    const root = this.element.nativeElement;
    const delta = autoScrollDelta(root.getBoundingClientRect(), event.clientY);
    if (delta) root.scrollTop += delta;
  }
  leave(event: DragEvent) {
    const next = event.relatedTarget;
    if (!(next instanceof Node) || !this.element.nativeElement.contains(next))
      this.hover(null);
  }
  drop(event: DragEvent) {
    if (!this.accepts(event)) return;
    event.preventDefault();
    const target = this.target(event.clientX, event.clientY);
    const drag = parseDrag(event.dataTransfer?.getData(REF_MIME) ?? "");
    const valid = this.owner === this.session() && this.owner?.isCurrent();
    this.end();
    if (!target || !drag || !valid) return;
    target.dispatchEvent(
      new CustomEvent<FieldDrop>(FIELD_DROP, {
        detail: {
          drag,
          x: event.clientX,
          y: event.clientY,
          shift: event.shiftKey,
        },
      }),
    );
  }
  cancel(event: Event) {
    if (!this.owner && !activeDrag()) return;
    event.preventDefault();
    event.stopPropagation();
    this.end();
  }
  end() {
    this.hover(null);
    this.owner = null;
    activeDrag.set(null);
  }
  private hover(next: HTMLElement | null) {
    if (next === this.hovered) return;
    this.hovered?.removeAttribute("data-drop-hover");
    next?.setAttribute("data-drop-hover", "");
    this.hovered = next;
  }
}
