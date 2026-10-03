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
// Data-reference field: an ARIA 1.2 editable combobox with list autocomplete
// and manual selection. Suggestions come from forms/core/scope.ts (typed,
// grouped by step); the person may still type any JSON pointer, and the
// compiler decides whether it is available.
import {
  Component,
  afterNextRender,
  ElementRef,
  computed,
  inject,
  input,
  linkedSignal,
  output,
  signal,
} from "@angular/core";
import { isPointer, parsePointer } from "../core/json";
import { compatibility, type Schema } from "../core/scope";
import { stepKindLabels } from "../../designer/step-kinds";
import { Icon } from "../../icon";
import {
  AnchoredPopover,
  pageOptionIndex,
  revealPopoverOption,
} from "./anchored-popover";

/** One suggestion; `ScopeEntry` from forms/core/scope.ts fits as is. */
export interface ReferenceOption {
  /** RFC 6901 pointer written as `{ref}`, for example `/steps/check/output/eligible`. */
  ref: string;
  source: "input" | "step";
  stepId?: string;
  stepKind?: string;
  label: string;
  /** "Workflow input › Customer ID" or "check › eligible". */
  breadcrumb: string;
  /** Plain type, for example "Text" or "Yes or no". */
  typeLabel: string;
  /** True when the value may be absent at run time. */
  optional: boolean;
  schema?: Schema;
}

export type Fit = "compatible" | "incompatible" | "unknown";
export interface ViewOption {
  option: ReferenceOption;
  fit: Fit;
  /** Position in the flat list; option ids and keyboard movement use it. */
  index: number;
}
export interface ViewGroup {
  id: string;
  label: string;
  /** The step kind, for the group's icon; none for the workflow input. */
  kind?: string;
  /** Plain step kind, for example "Call an integration". */
  detail?: string;
  options: ViewOption[];
}
export interface ReferenceView {
  groups: ViewGroup[];
  flat: ViewOption[];
}

const kindLabels: Record<string, string> = stepKindLabels;

/**
 * Filters and groups suggestions: the workflow input first, then each step in
 * scope order. Every query word must appear in the path, label, breadcrumb or
 * type. With a target schema, values that fit come first within a group and
 * the others are marked "incompatible".
 */
export function referenceView(
  options: readonly ReferenceOption[],
  query: string,
  target?: Schema | null,
): ReferenceView {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const groups = new Map<string, ViewGroup>();
  for (const option of options) {
    const text =
      `${option.ref} ${option.label} ${option.breadcrumb} ${option.typeLabel} ${option.stepId ?? ""}`.toLowerCase();
    if (!words.every((word) => text.includes(word))) continue;
    const id =
      option.source === "input" ? "input" : `step:${option.stepId ?? ""}`;
    let group = groups.get(id);
    if (!group) {
      group =
        option.source === "input"
          ? { id, label: "Workflow input", options: [] }
          : {
              id,
              label: option.stepId ?? "Step",
              kind: option.stepKind,
              detail: option.stepKind
                ? (kindLabels[option.stepKind] ?? option.stepKind)
                : undefined,
              options: [],
            };
      groups.set(id, group);
    }
    const fit: Fit =
      target && option.schema
        ? compatibility(option.schema, target)
        : "unknown";
    group.options.push({ option, fit, index: 0 });
  }
  const flat: ViewOption[] = [];
  const score = (item: ViewOption) =>
    (item.fit === "incompatible" ? 4 : item.fit === "unknown" ? 2 : 0) +
    (item.option.schema?.["type"] === "object" ||
    item.option.schema?.["type"] === "array"
      ? 1
      : 0);
  const ordered = [...groups.values()].sort(
    (a, b) =>
      Math.min(...a.options.map(score)) - Math.min(...b.options.map(score)),
  );
  for (const group of ordered) {
    group.options.sort((a, b) => score(a) - score(b));
    for (const item of group.options) {
      item.index = flat.length;
      flat.push(item);
    }
  }
  return { groups: ordered, flat };
}

/** Next active index for ArrowDown (+1) or ArrowUp (-1); wraps; -1 when empty. */
export function moveActive(current: number, delta: 1 | -1, count: number) {
  if (count <= 0) return -1;
  if (current < 0) return delta > 0 ? 0 : count - 1;
  return (current + delta + count) % count;
}

export interface ReferenceHint {
  tone: "info" | "warning" | "error";
  message: string;
}

/**
 * Plain feedback for typed text. Any well-formed pointer is accepted (the
 * compiler checks availability on Validate); the hint only explains.
 */
export function referenceHint(
  text: string,
  options: readonly ReferenceOption[],
): ReferenceHint | null {
  if (!text) return null;
  if (!text.startsWith("/"))
    return {
      tone: "error",
      message:
        "Start with a slash, for example /input/customerId or /steps/check/output.",
    };
  if (!isPointer(text))
    return {
      tone: "error",
      message: "Write “~” as ~0 and a slash inside a name as ~1.",
    };
  const known = options.find((o) => o.ref === text);
  if (known)
    return {
      tone: "info",
      message: known.optional
        ? `${known.typeLabel} · may be absent`
        : known.typeLabel,
    };
  const segments = parsePointer(text) ?? [];
  if (segments[0] !== "input" && segments[0] !== "steps")
    return {
      tone: "warning",
      message: "Data references start with /input or /steps/<step ID>/output.",
    };
  if (segments[0] === "steps" && segments.length >= 2) {
    const stepId = segments[1];
    if (!options.some((o) => o.stepId === stepId))
      return {
        tone: "warning",
        message: `No step named “${stepId}” is available here. Validate to check this reference.`,
      };
    if (segments[2] !== "output")
      return {
        tone: "warning",
        message: `Step data is under /steps/${stepId.replace(/~/g, "~0").replace(/\//g, "~1")}/output.`,
      };
  }
  return {
    tone: "info",
    message: "Not in the suggestions. Validate to check this reference.",
  };
}

let sequence = 0;

/**
 * Keyboard (ARIA 1.2 combobox, list autocomplete, manual selection): typing
 * filters and opens the list; ArrowDown/ArrowUp open it or move the active
 * suggestion (wrapping); Alt+ArrowDown opens and Alt+ArrowUp closes; Enter
 * picks the active suggestion (otherwise it keeps the typed text); Escape
 * closes the list; Tab leaves without picking. Focus stays in the text box;
 * `aria-activedescendant` names the active suggestion.
 */
@Component({
  selector: "weave-reference-combobox",
  standalone: true,
  imports: [Icon, AnchoredPopover],
  template: `<div class="ref-combo" (focusout)="focusOut($event)">
    <div
      #field
      class="ref-combo-field"
      [class.disabled]="disabled()"
      [class.has-token]="false"
    >
      @if (token(); as label) {
        <!-- The data in words ("Input › Customer ID"); the pointer under it. -->
        <span
          class="ref-combo-token sr-only"
          [id]="tokenId"
          [attr.title]="label"
          >{{ label }}</span
        >
      }
      <input
        type="text"
        role="combobox"
        autocomplete="off"
        autocapitalize="off"
        spellcheck="false"
        aria-autocomplete="list"
        [id]="inputId()"
        [attr.aria-label]="ariaLabel() || null"
        [attr.aria-expanded]="open()"
        [attr.aria-controls]="listId"
        [attr.aria-activedescendant]="
          open() && activeIndex() >= 0 ? optionId(activeIndex()) : null
        "
        [attr.aria-describedby]="describedBy()"
        [attr.aria-invalid]="hint()?.tone === 'error' ? 'true' : null"
        [placeholder]="placeholder()"
        [value]="!open() && token() && !showPaths() ? token() : text()"
        [disabled]="disabled()"
        (input)="typed($event)"
        (keydown)="keydown($event)"
      />
      <button
        type="button"
        class="ref-combo-toggle"
        tabindex="-1"
        [attr.aria-label]="
          'Show suggestions for ' + (ariaLabel() || 'this field')
        "
        [attr.aria-expanded]="open()"
        [attr.aria-controls]="listId"
        [disabled]="disabled()"
        (mousedown)="$event.preventDefault()"
        (click)="toggle()"
      >
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>
    </div>
    @if (removable()) {
      <button
        type="button"
        class="text-link"
        [disabled]="disabled()"
        (click)="removed.emit()"
      >
        Remove data
      </button>
    }
    @if (open() || showPaths()) {
      <button
        type="button"
        class="text-link"
        (mousedown)="$event.preventDefault()"
        (click)="showPaths.set(!showPaths())"
      >
        {{ showPaths() ? "Hide path" : "Show path" }}
      </button>
    }
    <!-- A press anywhere in the list (an option, a group heading, the
         scrollbar) keeps focus in the text box, so the list stays usable. -->
    <div
      class="ref-combo-list"
      [weaveAnchoredPopover]="open()"
      [popoverAnchor]="field"
      (popoverClosed)="close()"
      role="listbox"
      [id]="listId"
      [attr.aria-label]="listLabel()"
      [hidden]="!open()"
      (mousedown)="$event.preventDefault()"
    >
      @if (open()) {
        @for (group of view().groups; track group.id) {
          <div role="group" [attr.aria-labelledby]="listId + '-' + group.id">
            <div
              class="ref-combo-group"
              role="presentation"
              [id]="listId + '-' + group.id"
            >
              <weave-icon
                [name]="group.kind ?? 'source'"
                [size]="16"
                aria-hidden="true"
              />{{ group.label }}
              @if (group.detail) {
                <small>{{ group.detail }}</small>
              }
            </div>
            @for (item of group.options; track item.option.ref) {
              <div
                class="ref-combo-option"
                role="option"
                [id]="optionId(item.index)"
                [attr.aria-selected]="item.index === activeIndex()"
                [class.active]="item.index === activeIndex()"
                [class.current]="item.option.ref === text()"
                [attr.data-ref]="item.option.ref"
                [attr.title]="
                  item.option.breadcrumb + ' (' + item.option.ref + ')'
                "
                (mousemove)="activeIndex.set(item.index)"
                (click)="pick(item.option)"
              >
                <span class="ref-combo-name">{{ tokenText(item.option) }}</span>
                <span class="ref-combo-meta">
                  <span class="ref-combo-type">{{
                    item.option.typeLabel
                  }}</span>
                  @if (item.option.optional) {
                    <span class="ref-combo-note">may be absent</span>
                  }
                  @if (item.fit === "incompatible") {
                    <span class="ref-combo-warn">may not match this field</span>
                  }
                </span>
                @if (showPaths()) {
                  <code class="ref-combo-path">{{ item.option.ref }}</code>
                }
              </div>
            }
          </div>
        }
        @if (!view().flat.length) {
          <div class="ref-combo-empty" role="presentation">
            {{
              options().length
                ? "No suggestion matches. You can still type a path."
                : "No data is available here yet. You can still type a path."
            }}
          </div>
        }
      }
    </div>
    <p class="sr-only" aria-live="polite" aria-atomic="true">
      {{ announcement() }}
    </p>
    @if (hint(); as h) {
      <p
        class="ref-combo-hint"
        [class.warning]="h.tone === 'warning'"
        [class.error]="h.tone === 'error'"
        [id]="hintId"
      >
        {{ h.message }}
      </p>
    }
  </div>`,
  styles: [
    `
      :host {
        display: block;
        min-width: 0;
      }
      .ref-combo {
        position: relative;
      }
      .ref-combo-field {
        display: grid;
        grid-template-columns: minmax(0, 1fr) 36px;
        grid-template-areas: "input toggle";
        align-items: stretch;
        border: 1px solid var(--field-border);
        border-radius: var(--radius-sm);
        background: var(--surface);
      }
      .ref-combo-field:hover {
        border-color: var(--field-border-hover);
      }
      .ref-combo-field:focus-within {
        outline: 2px solid var(--focus);
        outline-offset: 1px;
        border-color: var(--focus);
      }
      .ref-combo-field.disabled {
        background: var(--disabled-bg);
        color: var(--disabled-ink);
        border-color: transparent;
      }
      .ref-combo-field.has-token {
        grid-template-areas:
          "token toggle"
          "input toggle";
      }
      .ref-combo-token {
        grid-area: token;
        padding: 5px 8px 0;
        font: 600 13px/18px var(--font-sans);
        color: var(--forest);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .ref-combo-field input {
        grid-area: input;
        min-width: 0;
        border: 0;
        font: var(--type-mono);
        color: var(--text);
      }
      /* The pointer is code; the prompt to choose is words. */
      .ref-combo-field input::placeholder {
        font-family: var(--font-sans);
      }
      .ref-combo-field.has-token input {
        min-height: 26px;
        padding-top: 0;
        color: var(--muted);
      }
      .ref-combo-field input:focus-visible {
        outline: none;
      }
      .ref-combo-toggle {
        grid-area: toggle;
        min-height: 0;
        width: 36px;
        padding: 0;
        border: 0;
        border-left: 1px solid var(--line);
        border-radius: 0 5px 5px 0;
      }
      .ref-combo-toggle svg {
        width: 16px;
        height: 16px;
        fill: none;
        stroke: currentColor;
        stroke-width: 2;
      }
      .ref-combo-list {
        color: var(--text);
        overflow: auto;
        overscroll-behavior: contain;
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        background: var(--surface);
        box-shadow: var(--shadow-2);
        padding: 4px;
      }
      .ref-combo-list[hidden] {
        display: none;
      }
      .ref-combo-group {
        display: flex;
        align-items: center;
        gap: 6px;
        font-size: 12px;
        font-weight: 600;
        color: var(--text);
        padding: 8px 6px 4px;
        overflow-wrap: anywhere;
      }
      .ref-combo-group weave-icon {
        color: var(--jade);
        flex: none;
      }
      .ref-combo-group small {
        font-weight: 400;
        font-size: 12px;
        color: var(--muted);
      }
      .ref-combo-option {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        gap: 2px 8px;
        padding: 6px;
        border-radius: 5px;
        border: 1px solid transparent;
        cursor: pointer;
      }
      .ref-combo-option.active {
        background: var(--hover);
        border-color: var(--border-hover);
      }
      .ref-combo-option.current .ref-combo-name {
        font-weight: 700;
      }
      /* Two lines at most: the data in words, then its pointer. */
      .ref-combo-name {
        font-size: 13px;
        min-width: 0;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .ref-combo-meta {
        display: flex;
        flex-wrap: nowrap;
        justify-content: flex-end;
        gap: 4px;
      }
      .ref-combo-type,
      .ref-combo-note,
      .ref-combo-warn {
        font-size: 12px;
        border-radius: var(--radius-pill);
        padding: 1px 7px;
        background: var(--mist);
        color: var(--text);
        white-space: nowrap;
      }
      .ref-combo-note {
        background: transparent;
        color: var(--muted);
      }
      .ref-combo-warn {
        background: var(--warning-bg);
        color: var(--warning-ink);
      }
      .ref-combo-path {
        grid-column: 1 / -1;
        font: var(--type-mono);
        color: var(--muted);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .ref-combo-empty {
        font-size: 12px;
        color: var(--muted);
        padding: 8px 6px;
      }
      .ref-combo-hint {
        margin: 4px 0 0;
        font-size: 12px;
        color: var(--muted);
        overflow-wrap: anywhere;
      }
      .ref-combo-hint.warning {
        color: var(--warning-ink);
      }
      .ref-combo-hint.error {
        color: var(--danger-ink);
      }
    `,
  ],
})
export class ReferenceCombobox {
  private readonly prefix = `ref-combo-${++sequence}`;
  /** The current pointer text. */
  value = input("");
  /** Suggestions, usually `visibleRefs(...)` or `referenceScope(...).entries`. */
  options = input<readonly ReferenceOption[]>([]);
  /** Schema of the field being bound; fitting values come first. */
  target = input<Schema | null>(null);
  /** Accessible name. Leave empty when a `<label for>` names `inputId`. */
  ariaLabel = input("Data reference");
  /** Id for the text box, so a visible `<label for>` can name it. */
  inputId = input(`${this.prefix}-input`);
  placeholder = input("Choose data…");
  removable = input(false);
  focusOnMount = input(false);
  removed = output<void>();
  readonly showPaths = signal(false);
  private readonly touched = signal(false);
  disabled = input(false);
  /** Extra ids for `aria-describedby`, for example a field hint. */
  describedByIds = input("");
  /** Every edit: each keystroke and each picked suggestion. */
  valueChange = output<string>();
  /** A suggestion was picked (click, or Enter on the active one). */
  picked = output<ReferenceOption>();

  readonly listId = `${this.prefix}-list`;
  readonly hintId = `${this.listId}-hint`;
  readonly tokenId = `${this.listId}-token`;
  /** The current data in words, when it is one of the suggestions. */
  readonly token = computed(() => {
    const text = this.text();
    const known = this.options().find((o) => o.ref === text);
    return known ? this.tokenText(known) : "";
  });
  /** "Input › Customer ID" or "load-order › rows". */
  tokenText(option: ReferenceOption) {
    return option.breadcrumb.replace(/^Workflow input(?= ›|$)/, "Input");
  }
  readonly text = linkedSignal(() => this.value());
  readonly open = signal(false);
  readonly activeIndex = signal(-1);
  /** Filtering applies only after the person types; opening shows everything. */
  private readonly query = signal("");
  readonly view = computed(() =>
    referenceView(this.options(), this.query(), this.target()),
  );
  /** Shown once the list is closed: while it is open, typing is a search. */
  readonly hint = computed(() =>
    this.open() || !this.touched()
      ? null
      : referenceHint(this.text(), this.options()),
  );
  /** "<field name> suggestions", from `ariaLabel` or the visible `<label for>`. */
  readonly listLabel = signal("Data reference suggestions");
  readonly describedBy = computed(
    () =>
      [
        this.token() ? this.tokenId : "",
        this.describedByIds(),
        this.hint() ? this.hintId : "",
      ]
        .filter(Boolean)
        .join(" ") || null,
  );
  readonly announcement = computed(() => {
    if (!this.open()) return "";
    const count = this.view().flat.length;
    return count === 1 ? "1 suggestion" : `${count} suggestions`;
  });

  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  constructor() {
    afterNextRender(() => {
      if (this.focusOnMount()) this.inputElement()?.focus();
    });
  }

  optionId(index: number) {
    return `${this.listId}-option-${index}`;
  }

  private nameList() {
    let name = this.ariaLabel();
    if (!name) {
      const id = CSS.escape(this.inputId());
      name =
        document
          .querySelector(`label[for="${id}"]`)
          ?.textContent?.replace(/\s+/g, " ")
          .trim() ?? "";
    }
    this.listLabel.set(`${name || "Data reference"} suggestions`);
  }

  typed(event: Event) {
    this.nameList();
    const text = (event.target as HTMLInputElement).value;
    this.text.set(text);
    this.query.set(text);
    this.activeIndex.set(-1);
    this.open.set(true);
    this.valueChange.emit(text);
  }

  /** Opens with every suggestion, the current value active when listed. */
  private show(activeFromEnd = false) {
    this.nameList();
    this.query.set("");
    this.open.set(true);
    const flat = this.view().flat;
    const current = flat.findIndex((o) => o.option.ref === this.text());
    this.activeIndex.set(
      current >= 0
        ? current
        : flat.length
          ? activeFromEnd
            ? flat.length - 1
            : 0
          : -1,
    );
    this.reveal();
  }

  close() {
    this.open.set(false);
    this.activeIndex.set(-1);
  }

  toggle() {
    if (this.open()) this.close();
    else this.show();
    this.inputElement()?.focus();
  }

  pick(option: ReferenceOption) {
    this.text.set(option.ref);
    this.close();
    this.valueChange.emit(option.ref);
    this.picked.emit(option);
    this.inputElement()?.focus();
  }

  keydown(event: KeyboardEvent) {
    const count = this.view().flat.length;
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        if (!this.open()) this.show();
        else if (!event.altKey) {
          this.activeIndex.set(moveActive(this.activeIndex(), 1, count));
          this.reveal();
        }
        return;
      case "ArrowUp":
        event.preventDefault();
        if (event.altKey) this.close();
        else if (!this.open()) this.show(true);
        else {
          this.activeIndex.set(moveActive(this.activeIndex(), -1, count));
          this.reveal();
        }
        return;
      case "Enter": {
        if (!this.open()) return;
        event.preventDefault();
        const active = this.view().flat[this.activeIndex()];
        if (active) this.pick(active.option);
        else this.close();
        return;
      }
      case "PageDown":
      case "PageUp": {
        if (!this.open()) return;
        event.preventDefault();
        const list =
          this.host.nativeElement.querySelector<HTMLElement>("[role=listbox]");
        if (list)
          this.activeIndex.set(
            pageOptionIndex(
              list,
              this.activeIndex(),
              event.key === "PageDown" ? 1 : -1,
            ),
          );
        this.reveal();
        return;
      }
      case "Escape":
        if (!this.open()) return;
        // Only the list closes; the inspector must not see this key.
        event.preventDefault();
        event.stopPropagation();
        this.close();
        return;
      case "Tab":
        this.close();
        return;
    }
  }

  focusOut(event: FocusEvent) {
    const next = event.relatedTarget as Node | null;
    if (!next || !this.host.nativeElement.contains(next)) {
      this.touched.set(true);
      const value = this.text().trim();
      if (/^(input|steps)\//.test(value)) {
        this.text.set("/" + value);
        this.valueChange.emit("/" + value);
      }
      this.close();
    }
  }

  private inputElement() {
    return this.host.nativeElement.querySelector(
      "input[role=combobox]",
    ) as HTMLInputElement | null;
  }

  /** Keeps the active suggestion visible after the list renders. */
  private reveal() {
    queueMicrotask(() =>
      requestAnimationFrame(() => {
        const index = this.activeIndex();
        if (index < 0) return;
        const element = this.host.nativeElement.querySelector(
          `#${CSS.escape(this.optionId(index))}`,
        ) as HTMLElement | null;
        const list =
          this.host.nativeElement.querySelector<HTMLElement>("[role=listbox]");
        if (list && element) revealPopoverOption(list, element);
      }),
    );
  }
}
