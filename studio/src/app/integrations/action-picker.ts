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
// Searchable picker over published actions: an ARIA 1.2 editable combobox
// (list autocomplete with automatic selection) with an anchored top-layer
// listbox, so a scrolling inspector or palette never clips it. It only
// emits choices; the host owns the catalog, its paging and the insert logic.
import {
  Component,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from "@angular/core";
import { Icon } from "../icon";
import { RowMenu, type RowMenuItem } from "../row-menu";
import {
  AnchoredPopover,
  pageOptionIndex,
  revealPopoverOption,
} from "../forms/ui/anchored-popover";

/** One published action version, enriched from its contract when loaded. */
export interface ActionPickerItem {
  id?: string;
  name: string;
  version: string;
  description?: string;
  /** implementation.uses of a connector action, for example weave-http@2.0.0. */
  connector?: string;
  /** taskType@taskVersion of a worker action. */
  worker?: string;
  /** What it calls: "GET /v1/pets/{petId}", or the connector's action name. */
  operation?: string;
  sideEffect?: "read_only" | "idempotent" | "non_idempotent";
}
export interface ActionPickerChoice {
  /** name@version, ready for the step's `uses`. */
  uses: string;
  id?: string;
  item: ActionPickerItem;
}
export interface ActionGroup {
  key: string;
  label: string;
  items: ActionPickerItem[];
}

const text = (value: unknown) =>
  typeof value === "string" && value.trim() ? value.trim() : undefined;
const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

/**
 * Picker entry from a definitions.list row plus, when the host has it, the
 * exported Action document. Retired or unreadable rows return null.
 */
export function actionPickerItem(
  row: Record<string, unknown>,
  document?: Record<string, unknown> | null,
): ActionPickerItem | null {
  const name = text(row["name"]);
  const version = text(row["version"]);
  if (!name || !version || row["retired"] || row["unavailable"]) return null;
  const item: ActionPickerItem = { name, version };
  const id = text(row["id"]);
  if (id) item.id = id;
  const spec = record(record(document)["spec"]);
  const implementation = record(spec["implementation"]);
  const description =
    text(record(spec["inputSchema"])["description"]) ??
    text(record(spec["outputSchema"])["description"]);
  if (description) item.description = description;
  if (implementation["kind"] === "connector") {
    const uses = text(implementation["uses"]);
    if (uses) item.connector = uses;
    const config = record(implementation["config"]);
    const method = text(config["method"]);
    const path = text(config["path"]);
    const action = text(implementation["action"]);
    if (method && path) item.operation = `${method} ${path}`;
    else if (action) item.operation = action;
  } else if (implementation["kind"] === "worker") {
    const task = text(implementation["taskType"]);
    const taskVersion = text(implementation["taskVersion"]);
    if (task) item.worker = taskVersion ? `${task}@${taskVersion}` : task;
  }
  const sideEffect = spec["sideEffect"];
  if (
    sideEffect === "read_only" ||
    sideEffect === "idempotent" ||
    sideEffect === "non_idempotent"
  )
    item.sideEffect = sideEffect;
  const bound = text(record(spec["connection"])["connector"]);
  if (!item.connector && bound) item.connector = bound;
  return item;
}

/** Group for an item: one per connector, one for workers, one for the rest. */
export function actionGroup(item: ActionPickerItem): {
  key: string;
  label: string;
} {
  if (item.connector) {
    if (item.connector.startsWith("weave-http@"))
      return { key: `connector:${item.connector}`, label: "HTTP APIs" };
    const name = item.connector.split("@")[0];
    return { key: `connector:${item.connector}`, label: `${name} connector` };
  }
  if (item.worker) return { key: "worker", label: "Worker actions" };
  return { key: "other", label: "Other actions" };
}

/** Semantic version precedence; build labels do not make a release newer. */
export function compareActionVersions(a: string, b: string): number {
  const parts = (value: string) => {
    const [core, ...suffix] = value.split("+")[0].split("-");
    return {
      core: core.split("."),
      pre: suffix.join("-").split(".").filter(Boolean),
    };
  };
  const left = parts(a),
    right = parts(b);
  const numeric = (x: string, y: string) =>
    x.length - y.length || (x < y ? -1 : x > y ? 1 : 0);
  for (let i = 0; i < 3; i++) {
    const order = numeric(left.core[i] ?? "0", right.core[i] ?? "0");
    if (order) return order;
  }
  if (!left.pre.length || !right.pre.length)
    return left.pre.length ? -1 : right.pre.length ? 1 : 0;
  for (let i = 0; i < Math.max(left.pre.length, right.pre.length); i++) {
    const x = left.pre[i],
      y = right.pre[i];
    if (x === undefined || y === undefined) return x === undefined ? -1 : 1;
    if (x === y) continue;
    const xn = /^\d+$/.test(x),
      yn = /^\d+$/.test(y);
    return xn && yn
      ? numeric(x, y)
      : xn !== yn
        ? xn
          ? -1
          : 1
        : x < y
          ? -1
          : 1;
  }
  return 0;
}

function latestActions(items: readonly ActionPickerItem[]): ActionPickerItem[] {
  const latest = new Map<string, ActionPickerItem>();
  for (const item of items) {
    const current = latest.get(item.name);
    const stable = !item.version.split("+")[0].includes("-");
    const currentStable =
      current && !current.version.split("+")[0].includes("-");
    if (
      !current ||
      (stable && !currentStable) ||
      (stable === currentStable &&
        compareActionVersions(item.version, current.version) > 0)
    )
      latest.set(item.name, item);
  }
  return [...latest.values()];
}

/** Most options rendered at once; the search narrows the rest. */
export const PICKER_LIMIT = 100;

/**
 * Filters by every word of the query over name, version, connector or
 * worker, operation and description, ranks name matches first and groups
 * the result by connector or worker. Each name@version is listed once (the
 * first occurrence wins), so overlapping catalog pages never repeat an
 * option or its id.
 */
export function searchActions(
  query: string,
  items: readonly ActionPickerItem[],
  limit = PICKER_LIMIT,
): { groups: ActionGroup[]; count: number; hidden: number } {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const ranked: { item: ActionPickerItem; score: number; index: number }[] = [];
  const seen = new Set<string>();
  const candidates = /\d+\.\d+\.\d+/.test(query) ? items : latestActions(items);
  candidates.forEach((item, index) => {
    const key = `${item.name}@${item.version}`;
    if (seen.has(key)) return;
    seen.add(key);
    const name = item.name.toLowerCase();
    const reference = key.toLowerCase();
    const wiring = [item.connector, item.worker, item.operation]
      .join(" ")
      .toLowerCase();
    const about = (item.description ?? "").toLowerCase();
    let score = 0;
    for (const word of words) {
      if (name.startsWith(word)) continue;
      if (reference.includes(word)) score = Math.max(score, 1);
      else if (wiring.includes(word)) score = Math.max(score, 2);
      else if (about.includes(word)) score = Math.max(score, 3);
      else return;
    }
    ranked.push({ item, score, index });
  });
  ranked.sort((a, b) => a.score - b.score || a.index - b.index);
  const shown = ranked.slice(0, limit);
  const groups = new Map<string, ActionGroup>();
  for (const { item } of shown) {
    const { key, label } = actionGroup(item);
    const group = groups.get(key) ?? { key, label, items: [] };
    group.items.push(item);
    groups.set(key, group);
  }
  return {
    groups: [...groups.values()],
    count: ranked.length,
    hidden: ranked.length - shown.length,
  };
}

type Option =
  | { kind: "action"; id: string; item: ActionPickerItem }
  | { kind: "more"; id: string }
  | { kind: "create"; id: string };

let sequence = 0;

/**
 * Keyboard: type to filter (the first match becomes active), ArrowDown and
 * ArrowUp open the list and move, Alt+ArrowDown opens it, Enter chooses,
 * Escape closes the list (or clears the search when it is closed), and Tab
 * leaves without choosing. Focus stays in the text field throughout: a
 * press anywhere in the list (an option, a group heading, the scrollbar)
 * never takes focus from it.
 */
@Component({
  selector: "weave-action-picker",
  standalone: true,
  imports: [Icon, AnchoredPopover, RowMenu],
  host: { "(focusout)": "focusOut($event)" },
  template: `<div class="action-picker">
    @if (empty()) {
      <p class="action-picker-label" [id]="prefix + '-label'">
        {{ label() }}
      </p>
      <div
        class="action-picker-empty"
        role="group"
        [id]="prefix + '-empty'"
        [attr.aria-labelledby]="prefix + '-label'"
      >
        @if (value()) {
          <p class="action-picker-value">
            This step uses <span class="monospace">{{ value() }}</span
            >, which isn't published in this project.
          </p>
        }
        <p>No published actions in this project yet.</p>
        @if (canCreate()) {
          <p>Describe an API request and Studio builds the action for you.</p>
          <button
            type="button"
            class="primary"
            [disabled]="disabled()"
            (click)="createAction.emit()"
          >
            <weave-icon name="plus" />New API action
          </button>
        } @else {
          <p>Ask a developer to publish an action, then refresh the list.</p>
        }
        <button type="button" class="text-link" (click)="retry.emit()">
          Refresh actions
        </button>
      </div>
    } @else {
      <label class="action-picker-label" [for]="prefix + '-input'">{{
        label()
      }}</label>
      <div #field class="search-field action-picker-field">
        <weave-icon name="search" />
        <input
          type="text"
          role="combobox"
          autocomplete="off"
          spellcheck="false"
          aria-autocomplete="list"
          [id]="prefix + '-input'"
          [attr.aria-expanded]="open()"
          [attr.aria-controls]="prefix + '-list'"
          [attr.aria-activedescendant]="
            open() && active() ? active()!.id : null
          "
          [attr.aria-describedby]="prefix + '-status'"
          [placeholder]="placeholder()"
          [disabled]="disabled()"
          [value]="text()"
          (input)="typed($event)"
          (keydown)="keydown($event)"
          (click)="openList()"
        />
        <button
          type="button"
          class="action-picker-toggle"
          tabindex="-1"
          [attr.aria-label]="open() ? 'Hide actions' : 'Show actions'"
          [attr.aria-expanded]="open()"
          [attr.aria-controls]="prefix + '-list'"
          [disabled]="disabled()"
          (mousedown)="$event.preventDefault()"
          (click)="toggle()"
        >
          <weave-icon name="chevron" />
        </button>
      </div>
      @if (current() && versions().length > 1 && !disabled()) {
        <weave-row-menu
          [label]="'Choose version of ' + current()!.name"
          [text]="'Version ' + current()!.version"
          icon="chevron"
          [items]="versionMenu()"
        />
      }
      <p class="sr-only" [id]="prefix + '-status'" aria-live="polite">
        {{ status() }}
      </p>
      <div
        class="action-picker-list"
        [weaveAnchoredPopover]="open()"
        [popoverAnchor]="field"
        (popoverClosed)="close()"
        role="listbox"
        [id]="prefix + '-list'"
        [attr.aria-label]="label()"
        [hidden]="!open()"
        (mousedown)="$event.preventDefault()"
      >
        @if (open()) {
          @for (group of results().groups; track group.key) {
            <div role="group" [attr.aria-labelledby]="prefix + '-' + group.key">
              <div
                class="action-picker-group"
                role="presentation"
                [id]="prefix + '-' + group.key"
              >
                {{ group.label }}
              </div>
              @for (item of group.items; track item.name + "@" + item.version) {
                <div
                  class="action-picker-option"
                  role="option"
                  [id]="optionId(item)"
                  [attr.aria-selected]="active()?.id === optionId(item)"
                  [class.active]="active()?.id === optionId(item)"
                  [class.current]="isCurrent(item)"
                  [attr.data-uses]="item.name + '@' + item.version"
                  (mousemove)="hover(optionId(item))"
                  (click)="pick(item)"
                >
                  <span class="action-picker-name">
                    <span
                      >{{ item.name
                      }}<span class="action-picker-version"
                        >&#64;{{ item.version }}</span
                      ></span
                    >
                    @if (isCurrent(item)) {
                      <span class="sr-only">(current)</span>
                      <weave-icon name="check" />
                    }
                  </span>
                  @if (item.sideEffect) {
                    <small>{{
                      item.sideEffect === "read_only"
                        ? "Reads data"
                        : "Changes data"
                    }}</small>
                  }
                  @if (details(item)) {
                    <small>{{ details(item) }}</small>
                  }
                </div>
              }
            </div>
          }
          @if (loading()) {
            <div class="action-picker-note" role="presentation">
              <span class="loading-spinner small"></span>Loading actions…
            </div>
          } @else if (!results().count) {
            <div class="action-picker-note" role="presentation">
              No action matches “{{ query() }}”.
            </div>
          }
          @if (results().hidden) {
            <div class="action-picker-note" role="presentation">
              {{ results().hidden }} more. Refine the search to see them.
            </div>
          }
          @if (hasMore()) {
            <div
              class="action-picker-option action-picker-command"
              role="option"
              [id]="prefix + '-more'"
              [attr.aria-selected]="active()?.id === prefix + '-more'"
              [attr.aria-disabled]="loadingMore()"
              [class.active]="active()?.id === prefix + '-more'"
              (mousemove)="hover(prefix + '-more')"
              (click)="more()"
            >
              <weave-icon name="refresh" />{{
                loadingMore() ? "Loading more actions…" : "Load more actions"
              }}
            </div>
          }
          @if (canCreate()) {
            <div
              class="action-picker-option action-picker-command"
              role="option"
              [id]="prefix + '-create'"
              [attr.aria-selected]="active()?.id === prefix + '-create'"
              [class.active]="active()?.id === prefix + '-create'"
              (mousemove)="hover(prefix + '-create')"
              (click)="create()"
            >
              <weave-icon name="plus" />New API action
            </div>
          }
        }
      </div>
    }
    @if (error()) {
      <div class="notice error-notice action-picker-error" role="alert">
        <p>{{ error() }}</p>
        <button type="button" (click)="retry.emit()">Try again</button>
      </div>
    }
  </div>`,
  styles: [
    `
      :host {
        display: block;
        min-width: 0;
      }
      .action-picker-label {
        margin: 0 0 8px;
        font-size: 12px;
        font-weight: 600;
      }
      .action-picker-value {
        color: var(--text);
        overflow-wrap: anywhere;
      }
      .action-picker-field {
        width: 100%;
        padding-right: 2px;
      }
      .action-picker-field:focus-within {
        outline: 2px solid var(--focus);
        outline-offset: 1px;
        border-color: var(--focus);
      }
      .action-picker-field input {
        flex: 1;
        min-width: 0;
      }
      .action-picker-field input:focus-visible {
        outline: none;
      }
      .action-picker-toggle {
        border: 0;
        background: transparent;
        min-height: 32px;
        padding: 4px;
        color: var(--muted);
      }
      .action-picker-toggle weave-icon {
        width: 16px;
        height: 16px;
        --icon-stroke: 1.5;
        transform: rotate(90deg);
      }
      .action-picker-toggle[aria-expanded="true"] weave-icon {
        transform: rotate(-90deg);
      }
      .action-picker-list {
        color: var(--text);
        overflow: auto;
        overscroll-behavior: contain;
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        background: var(--surface);
        box-shadow: var(--shadow-2);
        padding: 4px;
      }
      .action-picker-group {
        font: 600 12px/16px var(--font-sans);
        color: var(--muted);
        padding: 8px 6px 4px;
      }
      .action-picker-option {
        display: flex;
        flex-direction: column;
        gap: 2px;
        padding: 7px 8px;
        border-radius: var(--radius-sm);
        border: 1px solid transparent;
        cursor: pointer;
        min-width: 0;
      }
      .action-picker-option.active {
        background: var(--hover);
        border-color: var(--border-hover);
      }
      .action-picker-name {
        display: flex;
        align-items: center;
        gap: 4px;
        font-size: 13px;
        font-weight: 600;
        overflow-wrap: anywhere;
      }
      .action-picker-name weave-icon {
        width: 16px;
        height: 16px;
        --icon-stroke: 1.5;
        color: var(--muted);
      }
      .action-picker-version {
        color: var(--muted);
        font-weight: 400;
      }
      .action-picker-option small {
        color: var(--muted);
        font-size: 12px;
        overflow-wrap: anywhere;
      }
      .action-picker-command {
        flex-direction: row;
        align-items: center;
        gap: 8px;
        font-size: 13px;
        font-weight: 600;
        color: var(--text);
      }
      .action-picker-command weave-icon {
        width: 16px;
        height: 16px;
        --icon-stroke: 1.5;
      }
      .action-picker-note {
        font-size: 12px;
        color: var(--muted);
        padding: 6px 8px;
      }
      .action-picker-empty {
        border: 1px dashed var(--border);
        border-radius: var(--radius-md);
        padding: 12px;
        font-size: 12px;
        color: var(--muted);
      }
      .action-picker-empty p {
        margin: 0 0 8px;
      }
      .action-picker-empty button.primary {
        margin: 4px 0 8px;
      }
      .action-picker-error {
        margin-top: 8px;
      }
    `,
  ],
})
export class ActionPicker {
  /** Published action versions, ideally enriched with actionPickerItem(). */
  actions = input<readonly ActionPickerItem[]>([]);
  /** The `uses` reference currently chosen, for example crm.lookup@1.2.0. */
  value = input<string | null>(null);
  label = input("Published action");
  placeholder = input("Search by name, API or operation");
  loading = input(false);
  /** Plain-language load failure; shows a Try again button that emits retry. */
  error = input("");
  /** The catalog has another page; shows a "Load more actions" option. */
  hasMore = input(false);
  loadingMore = input(false);
  /** The person may publish actions: offers "New API action". */
  canCreate = input(false);
  disabled = input(false);

  choose = output<ActionPickerChoice>();
  createAction = output<void>();
  loadMore = output<void>();
  retry = output<void>();

  prefix = `action-picker-${++sequence}`;
  open = signal(false);
  query = signal("");
  text = signal("");
  activeId = signal<string | null>(null);

  current = computed(() => {
    const value = this.value();
    return value
      ? this.actions().find((a) => `${a.name}@${a.version}` === value)
      : undefined;
  });
  versions = computed(() => {
    const selected = this.current();
    const seen = new Set<string>();
    return selected
      ? this.actions()
          .filter(
            (item) =>
              item.name === selected.name &&
              !seen.has(item.version) &&
              !!seen.add(item.version),
          )
          .sort((a, b) => compareActionVersions(b.version, a.version))
      : [];
  });
  versionMenu = computed((): RowMenuItem[] =>
    this.versions().map((item) => ({
      label: item.version,
      detail: this.isCurrent(item)
        ? "Selected version"
        : "Use this published version",
      run: () => this.pick(item),
    })),
  );
  results = computed(() => searchActions(this.query(), this.actions()));
  options = computed((): Option[] => {
    const list: Option[] = this.results().groups.flatMap((group) =>
      group.items.map(
        (item): Option => ({ kind: "action", id: this.optionId(item), item }),
      ),
    );
    if (this.hasMore()) list.push({ kind: "more", id: `${this.prefix}-more` });
    if (this.canCreate())
      list.push({ kind: "create", id: `${this.prefix}-create` });
    return list;
  });
  active = computed(() =>
    this.options().find((option) => option.id === this.activeId()),
  );
  empty = computed(
    () =>
      !this.loading() &&
      !this.error() &&
      !this.actions().length &&
      !this.hasMore(),
  );
  status = computed(() => {
    if (!this.open()) return "";
    if (this.loading()) return "Loading actions";
    const count = this.results().count;
    return count === 1 ? "1 action" : `${count} actions`;
  });

  private host = inject<ElementRef<HTMLElement>>(ElementRef);

  constructor() {
    // Shows the chosen action whenever the list is closed.
    effect(() => {
      const value = this.value();
      const current = this.current();
      if (!this.open())
        this.text.set(
          current
            ? `${current.name}@${current.version}`
            : value === "your-action@1.0.0"
              ? ""
              : (value ?? ""),
        );
    });
  }

  optionId(item: ActionPickerItem) {
    return `${this.prefix}-option-${item.name}@${item.version}`.replace(
      /[^A-Za-z0-9_@.-]/g,
      "_",
    );
  }
  isCurrent(item: ActionPickerItem) {
    return this.value() === `${item.name}@${item.version}`;
  }
  details(item: ActionPickerItem) {
    return [item.operation, item.worker, item.description]
      .filter(Boolean)
      .join(" · ");
  }
  openList(query = "") {
    if (this.disabled() || this.open()) return;
    this.query.set(query);
    this.open.set(true);
    // Opening is exploratory. Typing or arrow navigation chooses an active row.
    this.activeId.set(query ? (this.options()[0]?.id ?? null) : null);
    this.scrollActive();
  }
  close(restoreText = true) {
    this.open.set(false);
    this.query.set("");
    this.activeId.set(null);
    if (restoreText) {
      const current = this.current();
      this.text.set(
        current
          ? `${current.name}@${current.version}`
          : this.value() === "your-action@1.0.0"
            ? ""
            : (this.value() ?? ""),
      );
    }
  }
  toggle() {
    if (this.open()) this.close();
    else {
      this.openList();
      this.inputElement()?.focus();
    }
  }
  typed(event: Event) {
    const value = (event.target as HTMLInputElement).value;
    this.text.set(value);
    this.query.set(value);
    if (!this.open()) this.open.set(true);
    this.activeId.set(this.options()[0]?.id ?? null);
    this.scrollActive();
  }
  hover(id: string) {
    if (this.activeId() !== id) this.activeId.set(id);
  }
  pick(item: ActionPickerItem) {
    const uses = `${item.name}@${item.version}`;
    this.close(false);
    this.text.set(uses);
    this.choose.emit(item.id ? { uses, id: item.id, item } : { uses, item });
  }
  more() {
    if (this.loadingMore()) return;
    this.loadMore.emit();
  }
  create() {
    this.close();
    this.createAction.emit();
  }
  keydown(event: KeyboardEvent) {
    const options = this.options();
    const index = options.findIndex((o) => o.id === this.activeId());
    switch (event.key) {
      case "ArrowDown":
      case "ArrowUp": {
        event.preventDefault();
        if (!this.open()) {
          this.openList();
          if (event.altKey) return;
          if (options.length)
            this.activeId.set(
              event.key === "ArrowUp"
                ? this.options().at(-1)!.id
                : this.options()[0].id,
            );
          this.scrollActive();
          return;
        }
        if (!options.length) return;
        const step = event.key === "ArrowDown" ? 1 : -1;
        const next =
          index < 0
            ? step > 0
              ? 0
              : options.length - 1
            : (index + step + options.length) % options.length;
        this.activeId.set(options[next].id);
        this.scrollActive();
        return;
      }
      case "Enter": {
        if (!this.open()) return;
        event.preventDefault();
        const option = this.active();
        if (!option) return;
        if (option.kind === "action") this.pick(option.item);
        else if (option.kind === "more") this.more();
        else this.create();
        return;
      }
      case "PageDown":
      case "PageUp": {
        if (!this.open()) return;
        event.preventDefault();
        const list =
          this.host.nativeElement.querySelector<HTMLElement>("[role=listbox]");
        if (list)
          this.activeId.set(
            options[
              pageOptionIndex(list, index, event.key === "PageDown" ? 1 : -1)
            ]?.id ?? null,
          );
        this.scrollActive();
        return;
      }
      case "Escape":
        if (this.open()) {
          event.preventDefault();
          // Keep the key from also closing a surrounding dialog or panel.
          event.stopPropagation();
          this.close();
        } else if (this.text() && this.text() !== this.value()) {
          event.preventDefault();
          event.stopPropagation();
          this.close();
        }
        return;
      case "Tab":
        if (this.open()) this.close();
        return;
    }
  }
  focusOut(event: FocusEvent) {
    const next = event.relatedTarget as Node | null;
    if (next && (this.host.nativeElement as HTMLElement).contains(next)) return;
    if (this.open()) this.close();
  }
  private inputElement() {
    return (
      this.host.nativeElement as HTMLElement
    ).querySelector<HTMLInputElement>("input[role=combobox]");
  }
  private scrollActive() {
    const id = this.activeId();
    if (!id) return;
    queueMicrotask(() =>
      requestAnimationFrame(() => {
        const list =
          this.host.nativeElement.querySelector<HTMLElement>("[role=listbox]");
        const option = document.getElementById(id);
        if (list && option) revealPopoverOption(list, option);
      }),
    );
  }
}
