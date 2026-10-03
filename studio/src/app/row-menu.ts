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
// A row's "⋯" menu: secondary and destructive row actions. A menu button
// with arrow keys, Home/End, Escape (focus returns to the button) and Tab
// (closes). The list is fixed to the viewport, so a scrolling table never
// clips it. Used by lazily loaded views only.
import {
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  inject,
  input,
  signal,
  viewChild,
} from "@angular/core";
import { Icon } from "./icon";

export interface RowMenuItem {
  label: string;
  /** A second line: why the item can't run now, for example. */
  detail?: string;
  /** Shown in the danger tone; for removals. */
  danger?: boolean;
  disabled?: boolean;
  run: () => void;
}

let sequence = 0;

@Component({
  selector: "weave-row-menu",
  standalone: true,
  imports: [Icon],
  host: {
    class: "row-menu",
    "(focusout)": "focusOut($event)",
    "(document:pointerdown)": "outside($event)",
    "(window:resize)": "close(false)",
  },
  template: `<button
      #toggle
      type="button"
      class="icon-button row-menu-toggle"
      aria-haspopup="menu"
      [attr.aria-expanded]="open()"
      [attr.aria-controls]="open() ? id + '-menu' : null"
      [attr.aria-label]="label()"
      (click)="toggleMenu()"
      (keydown)="toggleKey($event)"
    >
      <weave-icon [name]="icon()" [size]="20" />{{ text() }}
    </button>
    @if (open()) {
      <div
        class="row-menu-list"
        role="menu"
        [id]="id + '-menu'"
        [attr.aria-label]="label()"
        [style.top.px]="place().top"
        [style.bottom.px]="place().bottom"
        [style.right.px]="place().right"
        (keydown)="menuKey($event)"
      >
        @for (item of items(); track item.label) {
          <button
            type="button"
            role="menuitem"
            tabindex="-1"
            [class.danger]="item.danger"
            [attr.aria-disabled]="item.disabled ? 'true' : null"
            (click)="pick(item)"
          >
            <span>{{ item.label }}</span>
            @if (item.detail) {
              <small>{{ item.detail }}</small>
            }
          </button>
        }
      </div>
    }`,
  styles: [
    `
      :host {
        position: relative;
        display: inline-flex;
      }
      .row-menu-list {
        position: fixed;
        z-index: 30;
        min-width: 200px;
        max-width: min(320px, calc(100vw - 32px));
        max-height: calc(100dvh - 16px);
        overflow: auto;
        padding: var(--space-1);
        display: grid;
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        box-shadow: var(--shadow-2);
      }
      .row-menu-list button {
        display: grid;
        justify-items: start;
        gap: 2px;
        padding-block: 6px;
        justify-content: flex-start;
        border: 0;
        background: transparent;
        min-height: var(--control-md);
        font-weight: 500;
        text-align: left;
        white-space: normal;
      }
      .row-menu-list button:hover,
      .row-menu-list button:focus-visible {
        background: var(--hover);
      }
      .row-menu-list small {
        font: var(--type-caption);
        color: var(--muted);
      }
      .row-menu-list button.danger {
        color: var(--danger-ink);
      }
      .row-menu-list button.danger:hover {
        background: var(--danger-bg);
      }
    `,
  ],
})
export class RowMenu {
  label = input.required<string>();
  items = input<RowMenuItem[]>([]);
  /** The icon on the toggle; "more" (⋯) unless a menu needs another. */
  icon = input("more");
  /** Visible text on the toggle beside the icon; none by default. */
  text = input("");
  readonly id = `row-menu-${++sequence}`;
  open = signal(false);
  place = signal<{ top: number | null; bottom: number | null; right: number }>({
    top: 0,
    bottom: null,
    right: 0,
  });
  private toggle = viewChild<ElementRef<HTMLButtonElement>>("toggle");
  private host = inject<ElementRef<HTMLElement>>(ElementRef);
  private injector = inject(Injector);

  private entries() {
    return [
      ...this.host.nativeElement.querySelectorAll<HTMLElement>(
        '[role="menuitem"]',
      ),
    ];
  }
  private focusItem(index: number) {
    afterNextRender(
      () => {
        const entries = this.entries();
        entries[(index + entries.length) % entries.length]?.focus();
      },
      { injector: this.injector },
    );
  }
  /**
   * Below the toggle, or above it when the window has no room below (a
   * menu in a panel's footer, for example).
   */
  private measure() {
    const box = this.toggle()?.nativeElement.getBoundingClientRect();
    if (!box) return;
    const height =
      this.items().reduce((sum, item) => sum + (item.detail ? 56 : 40), 0) + 10;
    const below = window.innerHeight - box.bottom - 8;
    const up = below < height && box.top - 8 > below;
    this.place.set({
      top: up ? null : box.bottom + 4,
      bottom: up ? window.innerHeight - box.top + 4 : null,
      right: Math.max(8, window.innerWidth - box.right),
    });
  }
  toggleMenu() {
    if (this.open()) return this.close();
    this.measure();
    this.open.set(true);
    this.focusItem(0);
  }
  toggleKey(event: KeyboardEvent) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    this.measure();
    this.open.set(true);
    this.focusItem(event.key === "ArrowDown" ? 0 : -1);
  }
  menuKey(event: KeyboardEvent) {
    const entries = this.entries();
    const index = entries.indexOf(document.activeElement as HTMLElement);
    const move = (to: number) => {
      event.preventDefault();
      entries[(to + entries.length) % entries.length]?.focus();
    };
    if (event.key === "ArrowDown") move(index + 1);
    else if (event.key === "ArrowUp") move(index - 1);
    else if (event.key === "Home") move(0);
    else if (event.key === "End") move(entries.length - 1);
    else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      this.close();
    } else if (event.key === "Tab") this.close(false);
  }
  pick(item: RowMenuItem) {
    if (item.disabled) return;
    this.close();
    item.run();
  }
  close(returnFocus = true) {
    if (!this.open()) return;
    this.open.set(false);
    if (returnFocus) this.toggle()?.nativeElement.focus();
  }
  focusOut(event: FocusEvent) {
    const next = event.relatedTarget;
    if (next instanceof Node && this.host.nativeElement.contains(next)) return;
    if (next) this.close(false);
  }
  outside(event: PointerEvent) {
    if (!this.host.nativeElement.contains(event.target as Node))
      this.close(false);
  }
}
