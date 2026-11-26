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
// The item chip above a loop's frame on the Runs tab: which item of the
// loop the steps inside show ("Item 4 of 20"), Previous and Next, and a list
// of every item with what it did, the failed ones first, to choose one. On a
// phone it is one button, and the list is a sheet along the screen's bottom.
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  inject,
  input,
  output,
  signal,
  viewChild,
} from "@angular/core";
import { AnchoredPopover } from "../../forms/ui/anchored-popover";
import { Icon } from "../../icon";
import type { RecordedItem, RecordedLoop } from "./run-state";

const WORDS: Readonly<Record<RecordedItem["status"], string>> = {
  finished: "Finished",
  failed: "Failed",
  // Under "Failed, continued", the row's word says only what came next.
  handled: "Continued",
  running: "Running",
  "not-run": "Not run",
  stopped: "Stopped",
  "timed-out": "Timed out",
};
const ICONS: Readonly<Record<RecordedItem["status"], string>> = {
  finished: "ok",
  failed: "failCircle",
  handled: "degraded",
  running: "spinner",
  // The dashed circle: nothing ran.
  "not-run": "typeAny",
  // The run's own icons, as the Runs list draws a cancelled or timed-out run.
  stopped: "off",
  "timed-out": "schedule",
};

/** "Item 4 of 20", one-based; "Item 4" while the count isn't the run's. */
export function itemLabel(index: number, loop: RecordedLoop): string {
  return loop.known
    ? `Item ${index + 1} of ${loop.count.toLocaleString("en-US")}`
    : `Item ${index + 1}`;
}

/** The item button's name: "Item 4 of 20, Failed. Choose an item". */
export function chipName(index: number, loop: RecordedLoop): string {
  const item = loop.items[index];
  const word =
    item?.transaction ??
    (item?.status === "handled" ? HANDLED : WORDS[item?.status ?? "not-run"]);
  return `${itemLabel(index, loop)}, ${word}. Choose an item`;
}
const HANDLED = "Failed, continued";
/** What an item did, in the list: its transaction's end when it left its path ("Undone"), else its own. */
export function itemWord(item: RecordedItem): string {
  return item.transaction ?? WORDS[item.status];
}

/**
 * The item list's groups: the failed items, then the items that failed and
 * went on (their step's On error handled it), when any; then every item.
 */
export function itemGroups(
  loop: RecordedLoop,
): { title: string; items: readonly RecordedItem[] }[] {
  const failed = loop.items.filter((item) => item.status === "failed");
  const handled = loop.items.filter((item) => item.status === "handled");
  return [
    ...(failed.length ? [{ title: "Failed", items: failed }] : []),
    ...(handled.length ? [{ title: HANDLED, items: handled }] : []),
    { title: "All items", items: loop.items },
  ];
}

/** Go to item: the one-based number typed, as an index; null when it isn't one of the items. */
export function typedItem(text: string, count: number): number | null {
  if (!/^\s*\d+\s*$/.test(text)) return null;
  const number = Number(text);
  return number >= 1 && number <= count ? number - 1 : null;
}

@Component({
  selector: "weave-item-chip",
  standalone: true,
  imports: [Icon, AnchoredPopover],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `@let l = loop();
    @let at = index();
    <div class="item-chip" role="group" [attr.aria-label]="'Items of ' + l.id">
      <button
        type="button"
        class="chip-step"
        aria-label="Previous item"
        [attr.title]="at === 0 ? 'This is the first item.' : 'Previous item'"
        [attr.aria-disabled]="at === 0 || null"
        [attr.tabindex]="tabIndex()"
        (click)="step(-1)"
      >
        <weave-icon name="chevronLeft" [size]="16" />
      </button>
      <button
        #toggle
        type="button"
        class="chip-item"
        aria-haspopup="dialog"
        [attr.aria-expanded]="open()"
        [attr.aria-label]="name()"
        [attr.data-status]="status()"
        [attr.tabindex]="tabIndex()"
        (click)="toggleList()"
        (keydown)="toggleKey($event)"
      >
        <weave-icon class="chip-status" [name]="icon(status())" [size]="16" />
        <span>{{ label() }}</span>
        <weave-icon name="chevronDown" [size]="16" />
      </button>
      <button
        type="button"
        class="chip-step"
        aria-label="Next item"
        [attr.title]="
          at >= l.count - 1 ? 'This is the last item.' : 'Next item'
        "
        [attr.aria-disabled]="at >= l.count - 1 || null"
        [attr.tabindex]="tabIndex()"
        (click)="step(1)"
      >
        <weave-icon name="chevron" [size]="16" />
      </button>
    </div>
    @if (open()) {
      <div
        class="item-list"
        role="dialog"
        [attr.aria-label]="'Items of ' + l.id"
        [weaveAnchoredPopover]="open()"
        [popoverAnchor]="toggle"
        [popoverSheet]="true"
        (popoverClosed)="close(false)"
        (keydown)="listKey($event)"
        (focusout)="leave($event)"
      >
        <div class="item-list-head">
          <strong>Items of {{ l.id }}</strong>
          <button
            type="button"
            class="item-list-close"
            aria-label="Close"
            (click)="close(true)"
          >
            <weave-icon name="close" [size]="16" />
          </button>
        </div>
        <label class="item-go">
          <span>Go to item</span>
          <input
            #go
            type="text"
            inputmode="numeric"
            autocomplete="off"
            [value]="at + 1"
            (keydown.enter)="goTo(go.value, $event)"
          />
          @if (l.known) {
            <span>of {{ l.count.toLocaleString("en-US") }}</span>
          }
        </label>
        <div class="item-options" role="listbox" aria-label="Items">
          @for (group of groups(); track group.title) {
            <div
              class="item-group"
              role="group"
              [attr.aria-label]="group.title + ', ' + group.items.length"
            >
              <div class="item-group-head" aria-hidden="true">
                <span>{{ group.title }}</span
                ><span>{{ group.items.length.toLocaleString("en-US") }}</span>
              </div>
              @for (item of group.items; track item.index) {
                <button
                  type="button"
                  class="item-option"
                  role="option"
                  tabindex="-1"
                  [attr.aria-selected]="item.index === at"
                  [attr.data-index]="item.index"
                  [attr.data-status]="item.status"
                  (click)="choose(item.index)"
                >
                  <weave-icon
                    class="item-chosen"
                    name="check"
                    [size]="16"
                    [style.visibility]="item.index === at ? null : 'hidden'"
                  />
                  <weave-icon
                    class="item-status"
                    [name]="icon(item.status)"
                    [size]="16"
                  />
                  <span class="item-name">Item {{ item.index + 1 }}</span>
                  @if (item.code) {
                    <code class="item-code">{{ item.code }}</code>
                  }
                  <span class="item-word">{{ word(item) }}</span>
                </button>
              }
            </div>
          }
        </div>
      </div>
    }`,
  styles: [
    `
      :host {
        display: block;
      }
      .item-chip {
        display: inline-flex;
        align-items: center;
        gap: 1px;
        padding: 0;
        border: 1px solid var(--border);
        border-radius: var(--radius-sm);
        background: var(--raised);
        box-shadow: var(--shadow-1);
        white-space: nowrap;
      }
      .item-chip button {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 6px;
        min-height: 32px;
        padding: 0;
        border: 0;
        border-radius: calc(var(--radius-sm) - 1px);
        background: transparent;
        color: var(--text);
        font: var(--type-caption);
      }
      .item-chip button:hover:not([aria-disabled="true"]) {
        background: var(--hover);
      }
      .item-chip .chip-step {
        width: 32px;
      }
      .item-chip .chip-step[aria-disabled="true"] {
        color: var(--disabled-ink);
        cursor: default;
      }
      .item-chip .chip-item {
        min-width: 112px;
        padding: 0 8px;
      }
      [data-status="failed"] > .chip-status,
      .item-option[data-status="failed"] .item-status {
        color: var(--danger-ink);
      }
      [data-status="handled"] > .chip-status,
      .item-option[data-status="handled"] .item-status,
      .item-option[data-status="handled"] .item-code {
        color: var(--warning-ink);
      }
      [data-status="finished"] > .chip-status,
      .item-option[data-status="finished"] .item-status {
        color: var(--success-ink);
      }
      [data-status="not-run"] > .chip-status,
      .item-option[data-status="not-run"] .item-status,
      [data-status="stopped"] > .chip-status,
      .item-option[data-status="stopped"] .item-status,
      [data-status="timed-out"] > .chip-status,
      .item-option[data-status="timed-out"] .item-status {
        color: var(--neutral-ink);
      }
      /* A phone, or a coarse pointer: one 44 px button that opens a sheet. */
      @media (max-width: 767px), (pointer: coarse) {
        .item-chip button {
          min-height: 44px;
        }
        .item-chip .chip-step {
          width: 44px;
        }
      }
      @media (max-width: 767px) {
        .item-chip .chip-step {
          display: none;
        }
      }
      .item-list {
        display: grid;
        grid-template-rows: auto auto minmax(0, 1fr);
        max-height: 420px;
        padding: 0;
        overflow: hidden;
        border: 1px solid var(--border);
        border-radius: var(--radius-md);
        background: var(--raised);
        box-shadow: var(--shadow-2);
        color: var(--text);
      }
      .item-list-head {
        display: none;
        align-items: center;
        justify-content: space-between;
        padding: 12px 8px 4px 16px;
        font: var(--type-title-sm);
      }
      .item-list[data-side="sheet"] .item-list-head {
        display: flex;
      }
      .item-list-close {
        width: 44px;
        height: 44px;
        border: 0;
        background: transparent;
        color: var(--text);
      }
      .item-go {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 8px 12px;
        border-bottom: 1px solid var(--line);
        color: var(--muted);
        font: var(--type-small);
      }
      .item-go input {
        width: 64px;
        height: 32px;
        min-height: 32px;
        box-sizing: border-box;
      }
      .item-list[data-side="sheet"] .item-go input {
        width: 80px;
        height: 44px;
        min-height: 44px;
      }
      .item-options {
        overflow: auto;
        padding: 4px;
      }
      .item-group + .item-group {
        margin-top: 4px;
        padding-top: 4px;
        border-top: 1px solid var(--line);
      }
      .item-group-head {
        display: flex;
        justify-content: space-between;
        padding: 6px 8px;
        color: var(--muted);
        font: var(--type-caption);
      }
      .item-option {
        display: grid;
        grid-template-columns: 16px 16px auto 1fr auto;
        align-items: center;
        gap: 8px;
        width: 100%;
        min-height: 36px;
        padding: 0 8px;
        border: 0;
        border-radius: var(--radius-sm);
        background: transparent;
        color: var(--text);
        font: var(--type-small);
        text-align: left;
        content-visibility: auto;
        contain-intrinsic-size: auto 36px;
      }
      .item-list[data-side="sheet"] .item-option {
        min-height: 48px;
      }
      .item-option:hover,
      .item-option:focus-visible {
        background: var(--hover);
      }
      .item-option[aria-selected="true"] {
        background: var(--selected);
      }
      .item-chosen {
        color: var(--accent);
      }
      .item-code {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        color: var(--danger-ink);
        font: var(--type-caption);
        font-family: var(--font-mono);
      }
      .item-word {
        grid-column: 5;
        color: var(--muted);
        font: var(--type-caption);
      }
    `,
  ],
})
export class ItemChip {
  readonly loop = input.required<RecordedLoop>();
  /** The item the steps inside show, zero-based. */
  readonly index = input.required<number>();
  readonly tabIndex = input<0 | -1>(-1);
  /** An item was chosen, zero-based. */
  readonly picked = output<number>();

  readonly open = signal(false);
  private readonly toggle =
    viewChild.required<ElementRef<HTMLButtonElement>>("toggle");

  readonly label = computed(() => itemLabel(this.index(), this.loop()));
  readonly name = computed(() => chipName(this.index(), this.loop()));
  readonly status = computed(
    () => this.loop().items[this.index()]?.status ?? "not-run",
  );
  readonly groups = computed(() => itemGroups(this.loop()));

  icon(status: RecordedItem["status"]): string {
    return ICONS[status];
  }
  word(item: RecordedItem): string {
    return itemWord(item);
  }

  step(delta: -1 | 1): void {
    const next = this.index() + delta;
    if (next >= 0 && next < this.loop().count) this.picked.emit(next);
  }
  toggleList(): void {
    if (this.open()) this.close(true);
    else this.openList();
  }
  /** Enter, Space or ↓ open the list on the chosen item. */
  toggleKey(event: KeyboardEvent): void {
    if (!["Enter", " ", "ArrowDown"].includes(event.key) || this.open()) return;
    event.preventDefault();
    event.stopPropagation();
    this.openList();
  }
  private openList(): void {
    this.open.set(true);
    // The list takes focus on the chosen item once it is drawn.
    afterNextRender(() => this.options()[this.chosenOption()]?.focus(), {
      injector: this.injector,
    });
  }
  close(refocus: boolean): void {
    if (!this.open()) return;
    this.open.set(false);
    if (refocus) this.toggle().nativeElement.focus({ preventScroll: true });
  }
  choose(index: number): void {
    this.picked.emit(index);
    this.close(true);
  }
  goTo(text: string, event: Event): void {
    event.preventDefault();
    event.stopPropagation();
    const index = typedItem(text, this.loop().count);
    if (index !== null) this.choose(index);
  }
  /** ↑ ↓ Home End move, a digit goes to Go to item, Escape closes. */
  listKey(event: KeyboardEvent): void {
    const target = event.target as HTMLElement;
    const options = this.options();
    const at = options.indexOf(target as HTMLButtonElement);
    let next = -1;
    if (event.key === "Escape") {
      this.close(true);
    } else if (event.key === "ArrowDown")
      next = Math.min(at + 1, options.length - 1);
    else if (event.key === "ArrowUp") next = Math.max(at - 1, 0);
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = options.length - 1;
    else if (/^\d$/.test(event.key) && at >= 0) {
      const field = this.host.querySelector<HTMLInputElement>(".item-go input");
      if (field) {
        field.value = "";
        field.focus();
      }
      return;
    } else return;
    event.preventDefault();
    event.stopPropagation();
    if (next >= 0) options[next]?.focus();
  }
  /** Focus leaving the list and the chip closes the list. */
  leave(event: FocusEvent): void {
    const next = event.relatedTarget as Node | null;
    if (next && this.host.contains(next)) return;
    this.close(false);
  }

  /** The list lives in the chip's own DOM, shown in the top layer. */
  private readonly host =
    inject<ElementRef<HTMLElement>>(ElementRef).nativeElement;
  private readonly injector = inject(Injector);
  private options(): HTMLButtonElement[] {
    return [...this.host.querySelectorAll<HTMLButtonElement>(".item-option")];
  }
  /** The chosen item's first row: under Failed when it failed. */
  private chosenOption(): number {
    return Math.max(
      0,
      this.options().findIndex(
        (option) => option.getAttribute("aria-selected") === "true",
      ),
    );
  }
}
