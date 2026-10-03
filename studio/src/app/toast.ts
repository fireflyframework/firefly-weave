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
// newest replaces the previous one; it stays 6 seconds, longer while hovered
// or focused. Errors stay inline or in banners; toasts report outcomes.
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

/** How long a toast stays when nobody hovers or focuses it. */
export const toastDuration = 6000;

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
    const id = this.current()?.id;
    this.timer = setTimeout(() => this.dismiss(id), toastDuration);
  }
  private stop() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }
}

/**
 * Renders the toast region at the end of the shell. It sits beside the
 * sidebar and, in the designer, above the diagnostics strip.
 */
@Component({
  selector: "weave-toast-host",
  standalone: true,
  imports: [NgTemplateOutlet],
  host: {
    class: "toast-region",
    "[class.top]": "top()",
    "[style.--shell-left.px]": "left()",
    "[style.--toast-bottom.px]": "bottom()",
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
      </div>
    </ng-template>`,
})
export class ToastHost implements OnDestroy {
  toasts = inject(ToastService);
  left = signal(224);
  bottom = signal(0);
  /** The toast moves to the top while it would cover the focused control. */
  top = signal(false);
  private host = inject<ElementRef<HTMLElement>>(ElementRef);
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
    this.avoidFocus();
    requestAnimationFrame(() => this.avoidFocus());
  }
  /**
   * Never covers what has keyboard focus (WCAG 2.4.11): the toast sits at the
   * bottom, or at the top while the bottom would cover the focused control.
   * When both would, it takes the place that covers less of it.
   */
  avoidFocus() {
    const focused = document.activeElement;
    const region = this.host.nativeElement;
    if (
      !this.toasts.current() ||
      !(focused instanceof HTMLElement) ||
      focused === document.body ||
      region.contains(focused)
    )
      return this.place(false);
    const toast = region.querySelector(".toast");
    if (!toast) return;
    const box = toast.getBoundingClientRect();
    const target = focused.getBoundingClientRect();
    // The two places, 24 px from the edge (16 px on a phone); the top one
    // sits under the top bar (see .toast-region.top).
    const gap = window.innerWidth <= 767 ? 16 : 24;
    const bottomEdge = window.innerHeight - this.bottom() - gap;
    const topEdge = 72 + 16;
    const overlap = (from: number, to: number) =>
      target.right > box.left && target.left < box.right
        ? Math.max(0, Math.min(to, target.bottom) - Math.max(from, target.top))
        : 0;
    const atBottom = overlap(bottomEdge - box.height, bottomEdge);
    const atTop = overlap(topEdge, topEdge + box.height);
    this.place(atBottom > 0 && atTop < atBottom);
  }
  /** Applied at once, so the toast moves before the focus is painted. */
  private place(top: boolean) {
    this.top.set(top);
    this.host.nativeElement.classList.toggle("top", top);
  }
  /** The sidebar's width and the designer's diagnostics strip height. */
  measure() {
    const sidebar = document.querySelector(".sidebar");
    const strip = document.querySelector(".diagnostics");
    this.left.set(sidebar?.getBoundingClientRect().width ?? 0);
    this.bottom.set(strip?.getBoundingClientRect().height ?? 0);
  }
  ngOnDestroy() {
    this.toasts.dismiss();
  }
}
