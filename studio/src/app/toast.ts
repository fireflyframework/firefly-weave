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
// Visible outcomes: one toast at a time, bottom-left of the content area. The
// newest replaces the previous one; it stays 6 seconds, longer when its text
// takes longer to read, and as long as it is hovered or focused. Errors stay
// inline or in banners; toasts report outcomes.
import { NgTemplateOutlet } from "@angular/common";
import {
  Component,
  ElementRef,
  Injectable,
  OnDestroy,
  afterRenderEffect,
  inject,
  signal,
} from "@angular/core";

export interface ToastAction {
  label: string;
  run: () => void;
}
export interface ToastOptions {
  text: string;
  action?: ToastAction;
  tone?: "neutral" | "danger";
}
export interface Toast extends Required<Omit<ToastOptions, "action">> {
  id: number;
  action?: ToastAction;
}

/** The bars at the top of a phone sheet of step details (see ToastHost.measure). */
const SHEET_BARS =
  ".step-details .sd-top, .step-details .sd-panes > .sd-segments, .step-details .sd-panes > .sd-main > .sd-head";
/** How long a toast stays at least when nobody hovers or focuses it. */
export const toastDuration = 6000;
/**
 * How long a toast with this text stays: two seconds to notice it, then
 * about three words a second to read it, and never less than 6 seconds.
 */
export const toastTime = (text: string): number =>
  Math.max(
    toastDuration,
    2000 + Math.ceil((text.trim().split(/\s+/).length * 1000) / 3),
  );

@Injectable({ providedIn: "root" })
export class ToastService {
  readonly current = signal<Toast | null>(null);
  private sequence = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  /** Hovered or focused: the countdown waits. */
  private holds = new Set<"hover" | "focus">();

  show(options: ToastOptions) {
    this.current.set({
      id: ++this.sequence,
      text: options.text,
      action: options.action,
      tone: options.tone ?? "neutral",
    });
    this.holds.clear();
    this.countdown();
  }
  dismiss(id = this.current()?.id) {
    if (id === undefined || this.current()?.id !== id) return;
    this.stop();
    this.holds.clear();
    this.current.set(null);
  }
  /** Runs the toast's action, then closes it. */
  act() {
    const toast = this.current();
    if (!toast?.action) return;
    this.dismiss(toast.id);
    toast.action.run();
  }
  hold(reason: "hover" | "focus") {
    this.holds.add(reason);
    this.stop();
  }
  release(reason: "hover" | "focus") {
    this.holds.delete(reason);
    if (!this.holds.size && this.current()) this.countdown();
  }
  private countdown() {
    this.stop();
    const toast = this.current();
    this.timer = setTimeout(
      () => this.dismiss(toast?.id),
      toastTime(toast?.text ?? ""),
    );
  }
  private stop() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }
}

/**
 * Renders the toast region at the end of the shell. It sits beside the
 * sidebar and, in the designer, above the diagnostics strip. Over step
 * details it sits in their panes' bottom-left corner, inside the pane there.
 */
@Component({
  selector: "weave-toast-host",
  standalone: true,
  imports: [NgTemplateOutlet],
  host: {
    class: "toast-region",
    "[class.top]": "top()",
    "(window:resize)": "measure()",
    "(document:focusin)": "focusMoved()",
    "(document:scroll)": "avoidFocus()",
  },
  // The polite region stays in the page for its whole life: a screen reader
  // only reliably announces content that changes inside a region it already
  // knows. Each new toast is rendered afresh (tracked by its id), so the same
  // text twice is announced twice. A danger toast is an alert, which screen
  // readers announce as it appears.
  template: `<div class="toast-live" role="status" aria-atomic="true">
      @for (toast of shown("neutral"); track toast.id) {
        <ng-container *ngTemplateOutlet="body; context: { $implicit: toast }" />
      }
    </div>
    @for (toast of shown("danger"); track toast.id) {
      <div class="toast-live" role="alert" aria-atomic="true">
        <ng-container *ngTemplateOutlet="body; context: { $implicit: toast }" />
      </div>
    }
    <ng-template #body let-toast>
      <div
        class="toast"
        [attr.data-tone]="toast.tone === 'danger' ? 'danger' : null"
        (mouseenter)="toasts.hold('hover')"
        (mouseleave)="toasts.release('hover')"
        (focusin)="toasts.hold('focus')"
        (focusout)="toasts.release('focus')"
      >
        <span class="toast-text">{{ toast.text }}</span>
        @if (toast.action; as action) {
          <button type="button" (click)="toasts.act()">
            {{ action.label }}
          </button>
        }
        <button
          type="button"
          class="toast-dismiss"
          aria-label="Dismiss notification"
          (click)="toasts.dismiss(toast.id)"
        >
          ×
        </button>
      </div>
    </ng-template>`,
})
export class ToastHost implements OnDestroy {
  toasts = inject(ToastService);
  /** The toast moves to the top while it would cover the focused control. */
  top = signal(false);
  private host = inject<ElementRef<HTMLElement>>(ElementRef);
  /** Measures again when the pane under the toast changes size (a separator drag). */
  private resizes =
    typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(() => {
          this.measure();
          this.avoidFocus();
        });
  private observed: Element | null = null;
  /** The current toast when it belongs in the region of this tone. */
  shown(tone: "neutral" | "danger"): Toast[] {
    const toast = this.toasts.current();
    return toast && (toast.tone === "danger") === (tone === "danger")
      ? [toast]
      : [];
  }
  constructor() {
    afterRenderEffect(() => {
      this.toasts.current();
      this.measure();
      this.avoidFocus();
    });
  }
  /** Checks again once the browser has scrolled the control into view. */
  focusMoved() {
    // Step details may have opened or closed meanwhile.
    if (this.toasts.current()) this.measure();
    this.avoidFocus();
    requestAnimationFrame(() => this.avoidFocus());
  }
  /**
   * Never covers what has keyboard focus (WCAG 2.4.11): the toast sits at the
   * bottom, or at the top while the bottom would cover the focused control.
   * When both would, it takes the place that covers less of it. Both places
   * are measured where the stylesheet puts them.
   */
  avoidFocus() {
    const focused = document.activeElement;
    const region = this.host.nativeElement;
    if (
      !this.toasts.current() ||
      !(focused instanceof HTMLElement) ||
      focused === document.body
    )
      return this.place(false);
    // Keep an action under the pointer as it receives focus between down/up.
    if (region.contains(focused)) return;
    if (!region.querySelector(".toast")) return;
    const target = focused.getBoundingClientRect();
    const shown = region.classList.contains("top");
    const at = (top: boolean) => {
      region.classList.toggle("top", top);
      return region.getBoundingClientRect();
    };
    const bottom = at(false);
    const top = at(true);
    region.classList.toggle("top", shown);
    const overlap = (box: DOMRect) =>
      target.right > box.left && target.left < box.right
        ? Math.max(
            0,
            Math.min(box.bottom, target.bottom) - Math.max(box.top, target.top),
          )
        : 0;
    const atBottom = overlap(bottom);
    const atTop = overlap(top);
    const onTop = atBottom > 0 && atTop < atBottom;
    this.place(onTop);
    if (onTop ? atTop > 0 : atBottom > 0)
      ToastHost.reveal(focused, target, onTop, top, bottom);
  }
  /**
   * When both places would cover the focused control, the control scrolls
   * into the band the toast leaves free: under the toast at the top, down to
   * where the bottom place ends, or above the toast at the bottom, up to
   * where the top place starts. Nothing scrolls when the band is too short.
   */
  private static reveal(
    focused: HTMLElement,
    target: DOMRect,
    onTop: boolean,
    top: DOMRect,
    bottom: DOMRect,
  ) {
    const gap = 8;
    const band = onTop
      ? { start: top.bottom + gap, end: bottom.bottom }
      : { start: top.top, end: bottom.top - gap };
    if (target.height > band.end - band.start) return;
    const by = onTop ? target.top - band.start : target.bottom - band.end;
    for (
      let scroller = focused.parentElement;
      scroller;
      scroller = scroller.parentElement
    ) {
      const { overflowY } = getComputedStyle(scroller);
      if (
        /auto|scroll/.test(overflowY) &&
        scroller.scrollHeight > scroller.clientHeight
      ) {
        scroller.scrollBy({ top: by, behavior: "instant" });
        return;
      }
    }
  }
  /** Applied at once, so the toast moves before the focus is painted. */
  private place(top: boolean) {
    this.top.set(top);
    this.host.nativeElement.classList.toggle("top", top);
  }
  /**
   * What the stylesheet places the toast by, set at once so the places
   * measured next agree with it:
   * - beside the shell: the sidebar's width (--shell-left) and the
   *   designer's diagnostics strip height (--toast-bottom);
   * - over step details, which cover both: the width of the pane under the
   *   panes' bottom-left corner (--toast-pane), and the top of what scrolls
   *   in that pane (--toast-clear), so a toast at the top stays under the
   *   pane's own bars; on a phone sheet, the bottom of the bars that stay at
   *   its top.
   */
  measure() {
    let left = 0;
    let bottom = 0;
    let pane: number | null = null;
    let clear: number | null = null;
    let corner: Element | null = null;
    const details = document.querySelector<HTMLElement>(".step-details");
    if (details?.dataset["layout"] === "sheet") {
      clear = 0;
      for (const bar of document.querySelectorAll(SHEET_BARS))
        if (bar.getClientRects().length)
          clear = Math.max(clear, bar.getBoundingClientRect().bottom);
    } else if (details) {
      for (const shown of details.querySelectorAll(".sd-panes > .sd-pane"))
        if (
          shown.getClientRects().length &&
          (!corner ||
            shown.getBoundingClientRect().left <
              corner.getBoundingClientRect().left)
        )
          corner = shown;
      if (corner) {
        const box = corner.getBoundingClientRect();
        const scroller = corner.querySelector(
          ".sd-pane-body, .sd-tabpanel:not([hidden])",
        );
        pane = box.width;
        clear = scroller?.getBoundingClientRect().top ?? box.top;
      }
    } else {
      left =
        document.querySelector(".sidebar")?.getBoundingClientRect().width ?? 0;
      bottom =
        document.querySelector(".diagnostics")?.getBoundingClientRect()
          .height ?? 0;
    }
    const style = this.host.nativeElement.style;
    const set = (name: string, value: number | null) =>
      value === null
        ? style.removeProperty(name)
        : style.setProperty(name, `${Math.round(value * 100) / 100}px`);
    set("--shell-left", left);
    set("--toast-bottom", bottom);
    set("--toast-pane", pane);
    set("--toast-clear", clear ? Math.round(clear) : null);
    this.observe(this.toasts.current() ? corner : null);
  }
  private observe(element: Element | null) {
    if (element === this.observed) return;
    if (this.observed) this.resizes?.unobserve(this.observed);
    this.observed = element;
    if (element) this.resizes?.observe(element);
  }
  ngOnDestroy() {
    this.resizes?.disconnect();
    this.toasts.dismiss();
  }
}
