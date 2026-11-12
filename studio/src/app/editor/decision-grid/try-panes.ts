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
// Table view's Input and Output panes: trying the table reads left to right.
// Input holds the test values the table receives; Output shows the rule
// that matched and the result, Studio's live answer at once and Weave's
// check a moment later. Step details draws each in its pane.
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  inject,
  input,
  output,
} from "@angular/core";
import type { Json } from "../../forms/core/json";
import {
  schemaTypes,
  typeIcon,
  typeLabel,
  type Schema,
} from "../../forms/core/scope";
import { Icon } from "../../icon";
import { TaskForm } from "../../task-form";
import type { ResultView, TableTry } from "./table-try";
import type { TryCard, TryView } from "./try-view";

@Component({
  selector: "weave-table-input-pane",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, TaskForm],
  host: { class: "dg-pane" },
  styles: `
    :host {
      display: flex;
      flex-direction: column;
      flex: 1;
      min-width: 0;
      min-height: 0;
    }
    .dg-try-values {
      font: var(--type-caption);
      color: var(--muted);
    }
    .pill {
      margin-left: auto;
    }
    .dg-try-body {
      display: flex;
      flex-direction: column;
      gap: var(--space-4);
    }
    .dg-try-hint {
      margin: 0;
      font: var(--type-small);
      color: var(--muted);
    }
    .dg-try-reset {
      align-self: flex-start;
      min-height: var(--sd-control);
    }
    /* The fields follow step details' density (36 px with a fine pointer,
       44 px on touch); a choice's text box and arrow sit on its border. */
    :host ::ng-deep weave-task-form input:not([type="checkbox"]),
    :host ::ng-deep weave-task-form select,
    :host ::ng-deep weave-task-form button {
      min-height: var(--sd-control);
    }
    :host ::ng-deep weave-task-form .choice-field button {
      width: var(--sd-control);
      min-width: var(--sd-control);
      margin-block: -1px;
    }
    :host ::ng-deep weave-task-form .choice-field input {
      margin-block: -1px;
    }
  `,
  template: `
    <header class="sd-pane-head">
      <h3 class="sd-pane-title">Input</h3>
      @if (foldable()) {
        <button
          type="button"
          class="icon-button dg-fold"
          aria-label="Fold Input"
          title="Fold Input"
          (click)="fold.emit()"
        >
          <weave-icon class="dg-mirror" name="foldPane" [size]="16" />
        </button>
      }
    </header>
    <div class="pane-sub">
      <span class="dg-try-values">Test values</span>
      @if (state().testData()) {
        <span
          class="pill"
          [attr.data-tone]="state().edited() ? 'neutral' : 'info'"
          >{{ state().edited() ? "Edited" : "From test data" }}</span
        >
      }
    </div>
    <div class="sd-pane-body">
      <div class="dg-try-body">
        <weave-task-form
          [schema]="schema()"
          [initialData]="state().initial()"
          [typed]="true"
          choiceIcon="chevronDown"
          (dataChange)="state().change($event)"
        />
        <p class="dg-try-hint">
          Change a value to see which rule matches. These values are for trying
          the table; they aren't saved with it.
        </p>
        @if (state().edited()) {
          <button
            type="button"
            class="dg-try-reset"
            (click)="state().useTestData()"
          >
            Use the step's test data
          </button>
        }
      </div>
    </div>
  `,
})
export class TableInputPane {
  readonly state = input.required<TableTry>();
  readonly foldable = input(false);
  readonly fold = output<void>();
  schema(): Schema {
    return this.state().schema();
  }
}

const VIEWS: { id: ResultView; label: string; icon: string }[] = [
  { id: "table", label: "Table", icon: "tableView" },
  { id: "json", label: "JSON", icon: "jsonView" },
];

/** A result's fields as rows: in the result schema's order, then the rest. */
interface ResultRow {
  key: string;
  icon: string;
  type: string;
  text: string;
}

@Component({
  selector: "weave-table-output-pane",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  host: { class: "dg-pane" },
  styles: `
    :host {
      display: flex;
      flex-direction: column;
      flex: 1;
      min-width: 0;
      min-height: 0;
    }
    .pane-views {
      margin-left: auto;
    }
    .pane-sub {
      flex-wrap: nowrap;
    }
    .pane-sub .pill {
      flex: none;
    }
    .pane-sub .pill weave-icon {
      width: 14px;
      height: 14px;
    }
    .dg-try-count {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .dg-try-body {
      display: flex;
      flex-direction: column;
      gap: var(--space-4);
    }
    .dg-callout {
      display: flex;
      align-items: flex-start;
      gap: var(--space-2);
      margin: 0;
      padding: var(--space-3);
      border: 1px solid var(--danger-bd);
      border-radius: var(--radius-md);
      background: var(--danger-bg);
      font: var(--type-small);
      color: var(--text);
    }
    .dg-callout > weave-icon {
      flex: none;
      margin-top: 2px;
      color: var(--danger-ink);
    }
    .dg-callout[data-tone="warning"] {
      border-color: var(--warning-bd);
      background: var(--warning-bg);
    }
    .dg-callout[data-tone="warning"] > weave-icon {
      color: var(--warning-ink);
    }
    .dg-callout p {
      margin: 0;
    }
    .dg-callout strong {
      font-weight: 600;
    }
    .dg-cards {
      display: flex;
      flex-direction: column;
      gap: var(--space-3);
    }
    :host button.dg-rule-card {
      display: grid;
      grid-template-columns: 16px minmax(0, 1fr) auto;
      align-items: center;
      column-gap: var(--space-2);
      row-gap: var(--space-1);
      width: 100%;
      min-height: var(--sd-control);
      padding: 10px var(--space-3);
      border: 1px solid var(--success-bd);
      border-radius: var(--radius-md);
      background: var(--success-bg);
      color: var(--text);
      text-align: left;
      font: var(--type-label);
      cursor: pointer;
    }
    :host button.dg-rule-card:hover {
      border-color: var(--success);
    }
    :host button.dg-rule-card:focus-visible {
      outline: 2px solid var(--focus);
      outline-offset: 2px;
    }
    .dg-rule-card > weave-icon {
      color: var(--success-ink);
    }
    :host button.dg-rule-card[data-tone="danger"] {
      border-color: var(--danger-bd);
      background: var(--danger-bg);
    }
    :host button.dg-rule-card[data-tone="danger"]:hover {
      border-color: var(--danger);
    }
    .dg-rule-card[data-tone="danger"] > weave-icon {
      color: var(--danger-ink);
    }
    :host button.dg-rule-card[data-tone="neutral"] {
      border-color: var(--border);
      background: var(--sunken);
    }
    :host button.dg-rule-card[data-tone="neutral"]:hover {
      border-color: var(--border-hover);
    }
    .dg-rule-card[data-tone="neutral"] > weave-icon {
      color: var(--muted);
    }
    .dg-rule-card-title {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .dg-rule-card-when {
      grid-column: 2 / -1;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font: var(--type-small);
      color: var(--muted);
    }
    .dg-rule-card-go {
      color: var(--muted);
    }
    .dg-try-caption,
    .dg-try-note,
    .dg-try-none {
      margin: 0;
      font: var(--type-caption);
      color: var(--muted);
    }
    .dg-try-none {
      font: var(--type-small);
    }
    .dg-result-label {
      margin: 0;
    }
    .dg-results {
      display: flex;
      flex-direction: column;
      gap: var(--space-3);
    }
    table.dg-result {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      border-spacing: 0;
      overflow: hidden;
    }
    table.dg-result caption {
      padding-bottom: var(--space-1);
      text-align: left;
      font: var(--type-caption);
      color: var(--muted);
    }
    .dg-result th,
    .dg-result td {
      height: var(--sd-control);
      padding: 0 var(--space-3);
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: middle;
      font: var(--type-body);
      color: var(--text);
      overflow-wrap: anywhere;
    }
    .dg-result tr:last-child > * {
      border-bottom: 0;
    }
    .dg-result th {
      width: 1%;
      white-space: nowrap;
      border-right: 1px solid var(--line);
      background: var(--sunken);
      font: var(--type-label);
    }
    .dg-result th > span {
      display: inline-flex;
      align-items: center;
      gap: var(--space-2);
    }
    .dg-type-tile {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex: none;
      width: 20px;
      height: 20px;
      border: 1px solid var(--border);
      border-radius: var(--radius-xs);
      color: var(--muted);
    }
    .dg-type-tile weave-icon {
      width: 12px;
      height: 12px;
    }
    pre.dg-result {
      margin: 0;
      padding: var(--space-3);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      background: var(--sunken);
      font: var(--type-mono);
      color: var(--text);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
  `,
  template: `
    @let shown = state().grid.shown();
    <header class="sd-pane-head">
      <h3 class="sd-pane-title">Output</h3>
      <div
        class="pane-views"
        role="radiogroup"
        aria-label="Result view"
        (keydown)="viewKey($event)"
      >
        @for (choice of views; track choice.id) {
          <button
            type="button"
            role="radio"
            [attr.aria-checked]="state().resultView() === choice.id"
            [attr.tabindex]="state().resultView() === choice.id ? 0 : -1"
            (click)="state().setResultView(choice.id)"
          >
            <weave-icon [name]="choice.icon" [size]="16" />{{ choice.label }}
          </button>
        }
      </div>
      @if (foldable()) {
        <button
          type="button"
          class="icon-button dg-fold"
          aria-label="Fold Output"
          title="Fold Output"
          (click)="fold.emit()"
        >
          <weave-icon name="foldPane" [size]="16" />
        </button>
      }
    </header>
    <div class="pane-sub">
      @if (shown?.pill; as pill) {
        <span class="pill" [attr.data-tone]="pill.tone">
          @if (pill.text === "Checked by Weave") {
            <weave-icon name="ok" />
          }
          {{ pill.text }}</span
        >
      }
      <span class="pane-count dg-try-count">{{ shown?.count ?? "" }}</span>
    </div>
    <div class="sd-pane-body">
      @if (!shown || shown.kind === "empty" || shown.kind === "pending") {
        <div class="pane-empty">
          <span class="pane-empty-icon" aria-hidden="true"
            ><weave-icon name="execute" [size]="20"
          /></span>
          <h4>No result yet</h4>
          <p>Type a value in Input to see which rule matches.</p>
        </div>
      } @else {
        <div class="dg-try-body">
          @if (shown.callout; as callout) {
            <div class="dg-callout" [attr.data-tone]="callout.tone">
              <weave-icon name="warning" [size]="16" />
              <p>
                <strong>{{ callout.lead }}</strong>
                @if (callout.rest) {
                  {{ " " + callout.rest }}
                }
              </p>
            </div>
          }
          @if (shown.cards.length) {
            <div class="dg-cards">
              @for (card of shown.cards; track $index) {
                <button
                  type="button"
                  class="dg-rule-card"
                  [attr.data-tone]="card.tone"
                  [attr.aria-label]="cardLabel(card)"
                  (click)="goTo(card)"
                >
                  <weave-icon [name]="cardIcon(card)" [size]="16" /><span
                    class="dg-rule-card-title"
                    >{{ card.title }}</span
                  >
                  @if (card.tone === "danger") {
                    <weave-icon
                      class="dg-rule-card-go"
                      name="chevron"
                      [size]="16"
                    />
                  } @else {
                    <span></span>
                  }
                  @if (card.detail) {
                    <span class="dg-rule-card-when">{{ card.detail }}</span>
                  }
                </button>
              }
            </div>
          }
          @if (shown.caption) {
            <p class="dg-try-caption">{{ shown.caption }}</p>
          }
          @if (shown.result; as result) {
            <h4 class="pane-caption dg-result-label">Result</h4>
            @if ("none" in result) {
              <p class="dg-try-none">{{ result.none }}</p>
            } @else if (state().resultView() === "json") {
              <pre class="dg-result" tabindex="0" aria-label="Result as JSON">{{
                json(result.value)
              }}</pre>
            } @else {
              <div class="dg-results">
                @for (group of tables(result.value); track $index) {
                  <table class="dg-result">
                    @if (group.caption) {
                      <caption>
                        {{
                          group.caption
                        }}
                      </caption>
                    }
                    <tbody>
                      @for (row of group.rows; track row.key) {
                        <tr>
                          <th scope="row">
                            <span
                              ><span class="dg-type-tile" [title]="row.type"
                                ><weave-icon [name]="row.icon" /></span
                              >{{ row.key }}</span
                            >
                          </th>
                          <td>{{ row.text }}</td>
                        </tr>
                      }
                    </tbody>
                  </table>
                }
              </div>
            }
          }
          @if (shown.note) {
            <p class="dg-try-note">{{ shown.note }}</p>
          }
        </div>
      }
      <p class="sr-only" aria-live="polite">{{ announcement(shown) }}</p>
    </div>
  `,
})
export class TableOutputPane {
  readonly state = input.required<TableTry>();
  readonly foldable = input(false);
  readonly fold = output<void>();
  readonly views = VIEWS;
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);

  json(value: Json): string {
    return JSON.stringify(value, null, 2);
  }
  cardIcon(card: TryCard): string {
    return card.tone === "danger"
      ? "degraded"
      : card.tone === "success"
        ? "ok"
        : "info";
  }
  cardLabel(card: TryCard): string {
    const where =
      card.row === null ? "Go to Otherwise" : `Go to rule ${card.row + 1}`;
    return `${card.title}${card.detail ? `: ${card.detail}` : ""}. ${where}`;
  }
  goTo(card: TryCard) {
    const view = this.state().grid.view();
    if (!view) return;
    this.state().grid.goToRule(card.row ?? view.rows.length);
  }
  /** Arrow keys move between Table and JSON, as a radio group does. */
  viewKey(event: KeyboardEvent) {
    if (
      !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)
    )
      return;
    event.preventDefault();
    const next = this.state().resultView() === "table" ? "json" : "table";
    this.state().setResultView(next);
    setTimeout(() =>
      this.element.nativeElement
        .querySelector<HTMLElement>('.pane-views [aria-checked="true"]')
        ?.focus(),
    );
  }
  /** The result as tables of fields: one per matched rule with Collect all. */
  tables(value: Json): { caption: string | null; rows: ResultRow[] }[] {
    const shown = this.state().grid.shown();
    if (Array.isArray(value))
      return value.map((item, index) => ({
        caption: shown?.cards[index]?.title ?? `Item ${index + 1}`,
        rows: this.rows(item, this.itemSchema()),
      }));
    return [
      { caption: null, rows: this.rows(value, this.state().outputSchema()) },
    ];
  }
  private itemSchema(): Schema {
    const schema = this.state().outputSchema() as Record<string, unknown>;
    const items = schema["items"];
    return (items && typeof items === "object" ? items : schema) as Schema;
  }
  private rows(value: Json, schema: Schema): ResultRow[] {
    const properties = ((schema as Record<string, unknown>)["properties"] ??
      {}) as Record<string, Schema>;
    const describe = (key: string, field: Schema | undefined, item: Json) => {
      const types = field ? schemaTypes(field) : [];
      const type = field ? typeLabel(field) : "";
      return {
        key,
        type,
        icon: typeIcon(types.length ? types : [jsonType(item)], type),
        text: textOf(item),
      };
    };
    if (!value || typeof value !== "object" || Array.isArray(value))
      return [describe("Result", schema, value)];
    const keys = [
      ...Object.keys(properties).filter((key) => key in value),
      ...Object.keys(value).filter((key) => !(key in properties)),
    ];
    return keys.map((key) => describe(key, properties[key], value[key]));
  }
  /** Said after Weave's check. */
  announcement(shown: TryView | null): string {
    if (!shown || shown.pill?.text !== "Checked by Weave") return "";
    return shown.callout
      ? `Checked by Weave: ${shown.callout.lead}`
      : `Checked by Weave: ${shown.count}.`;
  }
}

function jsonType(value: Json): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  return typeof value;
}
function textOf(value: Json): string {
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}
