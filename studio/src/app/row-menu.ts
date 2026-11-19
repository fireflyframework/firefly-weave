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
// (closes). The list is in the top layer, so a scrolling table never
// clips it. A menu whose items say whether they are checked is a choice:
// radio items, a check on the chosen one, and it opens on that one. Used
// by lazily loaded views only.
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
import { Icon, type IconSize } from "./icon";
import { AnchoredPopover, pageOptionIndex } from "./forms/ui/anchored-popover";

export interface RowMenuItem {
  label: string;
  /** A second line: why the item can't run now, for example. */
  detail?: string;
  /** Shown in the danger tone; for removals. */
  danger?: boolean;
  disabled?: boolean;
  /**
   * Can't run now, for the reason in `detail`: marked disabled, yet picking
   * it still runs it, so a command can say why (as its toolbar button does).
   */
  blocked?: boolean;
  /**
   * Set on every item of a choice: the item is a radio item, and the checked
   * one shows a check. Left out, the item is a plain menu item.
   */
  checked?: boolean;
  run: () => void;
}

/** Shown as disabled: a disabled item, or a blocked one. */
export const markedDisabled = (item: RowMenuItem): boolean =>
  !!(item.disabled || item.blocked);
/** Picking it runs it: every item but a disabled one. */
export const runsWhenPicked = (item: RowMenuItem): boolean => !item.disabled;

let sequence = 0;

@Component({
  selector: "weave-row-menu",
  standalone: true,
  imports: [Icon, AnchoredPopover],
  host: {
    class: "row-menu",
    "(focusout)": "focusOut($event)",
    "(document:pointerdown)": "outside($event)",
  },
  template: `<button
      #toggle
      type="button"
      class="icon-button row-menu-toggle"
      aria-haspopup="menu"
      [attr.aria-expanded]="open()"
      [attr.aria-controls]="open() ? id + '-menu' : null"
      [attr.aria-label]="label()"
      [attr.tabindex]="tabIndex()"
      (click)="toggleMenu()"
      (keydown)="toggleKey($event)"
    >
      <weave-icon [name]="icon()" [size]="iconSize()" />{{ text() }}
    </button>
    @if (open()) {
      <div
        class="row-menu-list"
        [weaveAnchoredPopover]="open()"
        [popoverAnchor]="toggle"
        (popoverClosed)="close(false)"
        role="menu"
        [id]="id + '-menu'"
        [attr.aria-label]="label()"
        (keydown)="menuKey($event)"
      >
        @for (item of items(); track item.label) {
          <button
            type="button"
            [attr.role]="
              item.checked === undefined ? 'menuitem' : 'menuitemradio'
            "
            [attr.aria-checked]="item.checked ?? null"
            tabindex="-1"
            [class.danger]="item.danger"
            [attr.aria-disabled]="markedDisabled(item) ? 'true' : null"
            (click)="pick(item)"
          >
            <span>{{ item.label }}</span>
            @if (item.detail) {
              <small>{{ item.detail }}</small>
            }
            @if (item.checked) {
              <weave-icon name="check" [size]="16" />
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
        color: var(--text);
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
      /* A choice's items keep a 16 px column at the right for the check, so
         their labels line up whichever is checked. */
      .row-menu-list button[role="menuitemradio"] {
        grid-template-columns: minmax(0, 1fr) 16px;
        align-items: center;
        column-gap: var(--space-3);
      }
      .row-menu-list button[role="menuitemradio"] > :not(weave-icon) {
        grid-column: 1;
      }
      .row-menu-list button[role="menuitemradio"] > weave-icon {
        grid-column: 2;
        grid-row: 1;
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
  /** The toggle icon's size: 20 px, or 16 px beside text that names a choice. */
  iconSize = input<IconSize>(20);
  /** Visible text on the toggle beside the icon; none by default. */
  text = input("");
  tabIndex = input(0);
  readonly id = `row-menu-${++sequence}`;
  open = signal(false);
  private toggle = viewChild<ElementRef<HTMLButtonElement>>("toggle");
  private host = inject<ElementRef<HTMLElement>>(ElementRef);
  private injector = inject(Injector);

  private entries() {
    return [
      ...this.host.nativeElement.querySelectorAll<HTMLElement>(
        '[role="menuitem"], [role="menuitemradio"]',
      ),
    ];
  }
  /** The checked item's place in a choice; -1 in a plain menu. */
  private checkedIndex() {
    return this.items().findIndex((item) => item.checked);
  }
  private focusItem(index: number) {
    afterNextRender(
      () =>
        queueMicrotask(() => {
          const entries = this.entries();
          entries[(index + entries.length) % entries.length]?.focus();
        }),
      { injector: this.injector },
    );
  }
  toggleMenu() {
    if (this.open()) return this.close();
    this.open.set(true);
    this.focusItem(Math.max(this.checkedIndex(), 0));
  }
  toggleKey(event: KeyboardEvent) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    this.open.set(true);
    // A choice opens on what is chosen; a plain menu on its first or last item.
    const checked = this.checkedIndex();
    this.focusItem(checked >= 0 ? checked : event.key === "ArrowDown" ? 0 : -1);
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
    else if (event.key === "PageDown" || event.key === "PageUp") {
      const list =
        this.host.nativeElement.querySelector<HTMLElement>("[role=menu]");
      if (list)
        move(pageOptionIndex(list, index, event.key === "PageDown" ? 1 : -1));
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      this.close();
    } else if (event.key === "Tab") this.close(false);
  }
  readonly markedDisabled = markedDisabled;
  pick(item: RowMenuItem) {
    if (!runsWhenPicked(item)) return;
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
