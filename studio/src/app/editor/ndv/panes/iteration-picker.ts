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
// The iteration picker: one row per loop around the step step details show,
// innermost last, with the loop's name, Previous, the item ("Item 3 of 20")
// and Next. The item button opens Go to item. Items count from 1 for people
// and from 0 in instance keys.
import {
  afterNextRender,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  inject,
  Injector,
  input,
  output,
  signal,
} from "@angular/core";
import { Icon } from "../../../icon";
import { AnchoredPopover } from "../../../forms/ui/anchored-popover";
import type { Step } from "../../../model";
import { loopLimitText } from "../../canvas/loop-frame";
import type { Iterations, LoopCount, LoopPick } from "../../state/iterations";

/** One loop around the step, as its picker row shows it. */
export interface PickerLoop {
  loopId: string;
  /** "Loop" for the innermost loop, "Outer loop" for the ones around it. */
  role: "Loop" | "Outer loop";
  /** The picked item, from 0. */
  index: number;
  count: number;
  /** False until Execute or the current data says how many items there are. */
  known: boolean;
  /** The loop's Up to: no item past it runs. */
  limit: number;
}
export interface PickerView {
  item: string;
  /** The item button's accessible name. */
  name: string;
  /** Previous can't move. */
  first: boolean;
  /** Next can't move. */
  last: boolean;
  /** No item to pick: every control is off. */
  empty: boolean;
  /** The line under the row, or null. */
  note: string | null;
  /** "warning": the note says Execute fails here. */
  tone: "warning" | null;
}

const number = (value: number) => value.toLocaleString("en-US");

/** What one row says and which of its controls can move. */
export function pickerView(loop: PickerLoop): PickerView {
  const at = loop.index + 1;
  if (!loop.known) {
    const item = `Item ${number(at)}`;
    return {
      item,
      name: `${item}. Choose an item`,
      first: loop.index === 0,
      // Up to bounds the items a run reaches, as it bounds Go to item.
      last: loop.index >= loop.limit - 1,
      empty: false,
      note:
        loop.index === 0
          ? "Execute previous steps to see the items."
          : "The number of items shows once the previous steps have run.",
      tone: null,
    };
  }
  if (loop.count === 0)
    return {
      item: "No items",
      name: "No items",
      first: true,
      last: true,
      empty: true,
      note: "The list is empty: the steps inside don't run.",
      tone: null,
    };
  const item = `Item ${number(at)} of ${number(loop.count)}`;
  // More items than Up to: the platform fails the run at the loop.
  const over = loop.count > loop.limit;
  return {
    item,
    name: `${item}. Choose an item`,
    first: loop.index === 0,
    last: loop.index >= loop.count - 1,
    empty: false,
    note: over ? loopLimitText(loop.count, loop.limit) : null,
    tone: over ? "warning" : null,
  };
}

/** The rows for the picks around a step, outermost first, with their counts and Up to. */
export function pickerLoops(
  picks: readonly LoopPick[],
  counts: (loopId: string) => LoopCount,
  limit: (loopId: string) => number,
): PickerLoop[] {
  return picks.map(({ loopId, index }, i) => ({
    loopId,
    role: i === picks.length - 1 ? "Loop" : "Outer loop",
    index,
    ...counts(loopId),
    limit: limit(loopId),
  }));
}

/** The picker's rows for a step in step details, from the draft's picks and counts and each loop's Up to. */
export function loopRows(
  iterations: Iterations,
  target: string,
  step: (id: string) => Step | null,
): PickerLoop[] {
  return pickerLoops(
    iterations.picks(target),
    (loopId) => iterations.counts(loopId),
    (loopId) => {
      const limit = step(loopId)?.["maxItems"];
      return typeof limit === "number" && limit > 0 ? limit : 1000;
    },
  );
}

/**
 * The item picked in a loop and the loops around it, as people count it
 * ("3", nested "1 › 3"): up to the named loop, else the innermost; null when
 * the loop isn't one of the rows.
 */
export function itemPath(
  rows: readonly PickerLoop[],
  loopId?: string,
): string | null {
  const end =
    loopId === undefined
      ? rows.length - 1
      : rows.findIndex((row) => row.loopId === loopId);
  if (end < 0) return null;
  return rows
    .slice(0, end + 1)
    .map((row) => (row.index + 1).toLocaleString("en-US"))
    .join(" › ");
}

/** Which loop's item a formula reads: the innermost for item and index, the named one for loops; null when none. */
export function readLoop(
  pointers: readonly string[],
  rows: readonly PickerLoop[],
): PickerLoop | null {
  for (const pointer of pointers) {
    const [root, id] = pointer.slice(1).split("/");
    if (root === "item" || root === "index") return rows.at(-1) ?? null;
    if (root === "loops") {
      const named = id?.replace(/~1/g, "/").replace(/~0/g, "~");
      const row = rows.find((loop) => loop.loopId === named);
      if (row) return row;
    }
  }
  return null;
}

/** The item a typed number goes to (from 0), or null when it isn't one of the loop's items. */
export function goToItem(text: string, loop: PickerLoop): number | null {
  const typed = text.trim();
  if (!/^\d+$/.test(typed)) return null;
  const at = Number(typed);
  const shown = loop.known ? loop.count : loop.limit;
  return at >= 1 && at <= shown ? at - 1 : null;
}

@Component({
  selector: "weave-iteration-picker",
  standalone: true,
  imports: [Icon, AnchoredPopover],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { "[class.compact]": "compact()" },
  template: `@for (loop of loops(); track loop.loopId) {
      @let view = pickerView(loop);
      <div class="picker-row" [attr.data-loop]="loop.loopId">
        @if (!compact()) {
          <weave-icon class="picker-icon" name="forEach" [size]="16" />
          <span class="picker-name">
            <strong>{{ loop.loopId }}</strong>
            <span>{{ loop.role }}</span>
          </span>
        }
        <div
          class="picker-group"
          role="group"
          [attr.aria-label]="'Items of ' + loop.loopId"
        >
          <button
            type="button"
            class="picker-step"
            aria-label="Previous item"
            [attr.title]="
              view.first ? 'This is the first item.' : 'Previous item'
            "
            [attr.aria-disabled]="view.first || null"
            (click)="step(loop, view, -1)"
          >
            <weave-icon name="chevronLeft" [size]="16" />
          </button>
          <button
            #toggle
            type="button"
            class="picker-item"
            aria-haspopup="dialog"
            [attr.aria-expanded]="open() === loop.loopId"
            [attr.aria-label]="view.name"
            [attr.aria-disabled]="view.empty || null"
            (click)="toggleList(loop, view)"
          >
            <span>{{ view.item }}</span>
            <weave-icon name="chevronDown" [size]="16" />
          </button>
          <button
            type="button"
            class="picker-step"
            aria-label="Next item"
            [attr.title]="view.last ? 'This is the last item.' : 'Next item'"
            [attr.aria-disabled]="view.last || null"
            (click)="step(loop, view, 1)"
          >
            <weave-icon name="chevron" [size]="16" />
          </button>
          @if (open() === loop.loopId) {
            <div
              class="picker-list"
              role="dialog"
              [attr.aria-label]="'Items of ' + loop.loopId"
              [weaveAnchoredPopover]="true"
              [popoverAnchor]="toggle"
              (popoverClosed)="close(false)"
              (keydown.escape)="close(true, $event)"
            >
              <label class="picker-go">
                <span>Go to item</span>
                <input
                  #go
                  type="text"
                  inputmode="numeric"
                  autocomplete="off"
                  [value]="loop.index + 1"
                  [attr.aria-invalid]="invalid() || null"
                  (keydown.enter)="goTo(loop, go.value, $event)"
                />
                @if (loop.known) {
                  <span>of {{ number(loop.count) }}</span>
                }
              </label>
              @if (invalid()) {
                <p class="picker-invalid" role="alert">{{ invalid() }}</p>
              }
            </div>
          }
        </div>
      </div>
      @if (view.note && !compact()) {
        <p class="picker-note" [attr.data-tone]="view.tone">
          @if (view.tone === "warning") {
            <weave-icon name="warning" [size]="16" />
          }
          {{ view.note }}
        </p>
      }
    }
    <p class="picker-announce" role="status">{{ announced() }}</p>`,
  styles: `
    :host {
      display: block;
      border-bottom: 1px solid var(--border);
    }
    .picker-row {
      display: flex;
      align-items: center;
      gap: 8px;
      min-block-size: 44px;
      padding: 4px 16px;
    }
    .picker-row + .picker-row,
    .picker-note + .picker-row {
      border-top: 1px dashed var(--border);
    }
    .picker-icon {
      color: var(--muted);
      flex: none;
    }
    .picker-name {
      display: flex;
      flex-direction: column;
      min-inline-size: 0;
      flex: 1;
      line-height: 1.2;
    }
    .picker-name strong {
      font-weight: 500;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .picker-name span {
      color: var(--muted);
      font-size: 12px;
    }
    .picker-group {
      display: flex;
      flex: none;
      border: 1px solid var(--border);
      border-radius: 6px;
    }
    .picker-group button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      min-inline-size: 32px;
      min-block-size: 32px;
      padding: 0 8px;
      border: 0;
      background: transparent;
      color: var(--text);
      font: inherit;
      cursor: pointer;
    }
    .picker-item {
      min-inline-size: 112px;
      border-inline: 1px solid var(--border) !important;
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }
    .picker-group button[aria-disabled="true"] {
      color: var(--disabled-ink);
      cursor: default;
    }
    .picker-group button:focus-visible {
      outline: 2px solid var(--focus);
      outline-offset: -2px;
    }
    @media (hover: hover) {
      .picker-group button:not([aria-disabled="true"]):hover {
        background: var(--hover);
      }
    }
    /* On touch, and on any screen under 768 px: 44 px buttons, a 128 px item. */
    @media (pointer: coarse), (max-width: 767px) {
      .picker-group button {
        min-inline-size: 44px;
        min-block-size: 44px;
      }
      /* Past .picker-group button's own minimum. */
      .picker-group .picker-item {
        min-inline-size: 128px;
      }
      .picker-row {
        min-block-size: 56px;
      }
    }
    .picker-note {
      margin: -4px 0 0;
      padding: 0 16px 8px 40px;
      color: var(--muted);
      font-size: 12px;
    }
    .picker-note[data-tone="warning"] {
      display: flex;
      gap: 6px;
      color: var(--warning-ink);
    }
    .picker-note weave-icon {
      flex: none;
    }
    .picker-list {
      padding: 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--raised);
      color: var(--text);
    }
    .picker-go {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .picker-go input {
      inline-size: 6ch;
      min-block-size: 32px;
      padding: 0 8px;
      border: 1px solid var(--field-border);
      border-radius: 6px;
      background: var(--sunken);
      color: var(--text);
      font: inherit;
      font-variant-numeric: tabular-nums;
    }
    .picker-go input[aria-invalid="true"] {
      border-color: var(--danger);
    }
    .picker-invalid {
      margin: 8px 0 0;
      color: var(--danger-ink);
      font-size: 12px;
    }
    /* The Result panel's source line: the buttons only, 24 px. */
    :host(.compact) {
      display: inline-block;
      /* At the right end of the Result panel's head, after its type. */
      margin-inline-start: auto;
      border-bottom: 0;
    }
    :host(.compact) .picker-row {
      min-block-size: 0;
      padding: 0;
    }
    :host(.compact) .picker-group button {
      min-inline-size: 24px;
      min-block-size: 24px;
      padding: 0 4px;
      font-size: 12px;
    }
    :host(.compact) .picker-item {
      min-inline-size: 88px;
    }
    .picker-announce {
      position: absolute;
      inline-size: 1px;
      block-size: 1px;
      overflow: hidden;
      clip-path: inset(50%);
      white-space: nowrap;
    }
  `,
})
export class IterationPicker {
  readonly loops = input.required<readonly PickerLoop[]>();
  /** The Result panel's source line: Previous, the item and Next only. */
  readonly compact = input(false);
  readonly picked = output<{ loopId: string; index: number }>();
  /** The loop whose Go to item is open. */
  readonly open = signal<string | null>(null);
  readonly invalid = signal<string | null>(null);
  /** What the polite live region last said: the item picked. */
  readonly announced = signal("");
  readonly pickerView = pickerView;
  readonly number = number;
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);

  step(loop: PickerLoop, view: PickerView, by: -1 | 1): void {
    if (view.empty || (by < 0 ? view.first : view.last)) return;
    this.pick(loop, loop.index + by);
  }
  toggleList(loop: PickerLoop, view: PickerView): void {
    if (view.empty) return;
    this.invalid.set(null);
    this.open.set(this.open() === loop.loopId ? null : loop.loopId);
    // Once the list is drawn: Go to item takes focus, so Escape reaches the list.
    if (this.open())
      afterNextRender(
        () => {
          const go =
            this.element.nativeElement.querySelector<HTMLInputElement>(
              ".picker-go input",
            );
          go?.focus();
          go?.select();
        },
        { injector: this.injector },
      );
  }
  goTo(loop: PickerLoop, text: string, event: Event): void {
    event.preventDefault();
    const index = goToItem(text, loop);
    if (index === null) {
      this.invalid.set(
        loop.known
          ? `Type a number from 1 to ${number(loop.count)}.`
          : "Type an item number, from 1.",
      );
      return;
    }
    this.close(true);
    this.pick(loop, index);
  }
  close(returnFocus: boolean, event?: Event): void {
    event?.preventDefault();
    event?.stopPropagation();
    const was = this.open();
    this.open.set(null);
    this.invalid.set(null);
    if (returnFocus && was)
      this.element.nativeElement
        .querySelector<HTMLElement>(
          `[data-loop="${CSS.escape(was)}"] .picker-item`,
        )
        ?.focus();
  }
  private pick(loop: PickerLoop, index: number): void {
    this.picked.emit({ loopId: loop.loopId, index });
    this.announced.set(pickerView({ ...loop, index }).item);
  }
}
