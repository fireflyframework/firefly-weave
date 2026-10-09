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
// The Output pane: the shape later steps can map (Schema), and what this
// step returns once it runs (Table and JSON, which wait for data). Steps
// with no output say why.
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  inject,
  input,
  signal,
} from "@angular/core";
import { Icon } from "../../../icon";
import { DataTree } from "./data-tree";
import {
  countLabel,
  sampleCount,
  filterRows,
  noOutputSentence,
  radioKey,
  savedView,
  writeView,
  defaultOutputView,
  schemaRows,
  type DataView,
} from "./views";
import type { FormSession } from "../params/form-session";
import { ndvRegistry, type Json } from "../registry";

@Component({
  selector: "weave-output-pane",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, DataTree],
  template: `@let none = noOutput();
    @let rows = this.rows();
    @let shown = filter(rows);
    <div class="pane-tools">
      <div
        class="pane-views"
        role="radiogroup"
        aria-label="Output view"
        (keydown)="radioKey($event)"
      >
        @for (choice of views; track choice.id) {
          <button
            type="button"
            role="radio"
            [attr.aria-checked]="view() === choice.id"
            [attr.tabindex]="view() === choice.id ? 0 : -1"
            (click)="setView(choice.id)"
          >
            <weave-icon [name]="choice.icon" [size]="16" />{{ choice.label }}
          </button>
        }
      </div>
      <button
        type="button"
        class="icon-button pane-search-toggle"
        aria-label="Search output"
        [attr.aria-expanded]="searching()"
        (click)="toggleSearch()"
      >
        <weave-icon name="search" [size]="16" />
      </button>
    </div>
    @if (searching()) {
      <input
        class="param-input pane-search"
        aria-label="Search output"
        placeholder="Search output"
        [value]="query()"
        (input)="query.set($any($event.target).value)"
        (keydown.escape)="closeSearch($event)"
      />
    }
    <div class="pane-sub">
      @if (event() !== undefined && value() === undefined) {
        <span class="pill" data-tone="info">Sample</span>
      }
      <span class="pane-count">{{
        none
          ? ""
          : event() !== undefined && view() !== "schema"
            ? sampleCount(event())
            : countLabel(rows.length, query() ? shown.length : undefined)
      }}</span>
    </div>
    <div class="pane-content">
      @if (none) {
        <p class="pane-empty-text">{{ none }}</p>
      } @else if (event() !== undefined && view() !== "schema") {
        @if (view() === "table") {
          <p class="pane-empty-text">
            Table view isn't available for this sample yet.
          </p>
          <button type="button" (click)="setView('json')">View JSON</button>
        } @else {
          <pre class="pane-json" tabindex="0" aria-label="Output data">{{
            json(event())
          }}</pre>
        }
      } @else if (view() === "schema") {
        @if (session().target === "$trigger" && event() === undefined) {
          <p class="pane-empty-text">No test event yet</p>
        }
        @if (rows.length) {
          <weave-data-tree [rows]="shown" label="Output fields" />
        } @else {
          <p class="pane-empty-text">This step's output can be any value.</p>
        }
      } @else {
        <div class="pane-empty">
          <span class="pane-empty-icon" aria-hidden="true"
            ><weave-icon name="execute" [size]="20"
          /></span>
          <h4>
            {{
              session().target === "$trigger"
                ? "No test event yet"
                : "No output yet"
            }}
          </h4>
          <p>
            Schema shows the fields later steps can read. Executing this step
            isn’t available yet.
          </p>
        </div>
      }
    </div>`,
})
export class OutputPane {
  session = input.required<FormSession>();
  /** What the step returned, once test data or a simulation provides it. */
  value = input<Json | undefined>(undefined);
  readonly chosenView = signal<DataView | undefined>(savedView("output"));
  view(): DataView {
    return this.chosenView() ?? defaultOutputView(this.event());
  }
  readonly searching = signal(false);
  readonly query = signal("");
  readonly countLabel = countLabel;
  readonly sampleCount = sampleCount;
  readonly views: { id: DataView; label: string; icon: string }[] = [
    { id: "schema", label: "Schema", icon: "schemaView" },
    { id: "table", label: "Table", icon: "tableView" },
    { id: "json", label: "JSON", icon: "jsonView" },
  ];
  readonly radioKey = radioKey;
  private readonly lifetime = inject(DestroyRef);
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);

  noOutput(): string | null {
    const step = this.session().controller.step(this.session().target);
    return step ? noOutputSentence(step) : null;
  }
  /** The step's data: its output once known, and the trigger's test event. */
  event(): Json | undefined {
    if (this.value() !== undefined) return this.value();
    return this.session().target === "$trigger"
      ? this.session().controller.testEvent()
      : undefined;
  }
  rows() {
    const session = this.session();
    const target = session.target;
    if (target === "$trigger")
      return schemaRows(session.host.model.definition.spec["inputSchema"]);
    if (target === "$end")
      return schemaRows(session.host.model.definition.spec["outputSchema"]);
    const step = session.controller.step(target);
    const schema = step
      ? ndvRegistry
          .kind(step.kind)
          ?.outputSchema(step, session.controller.kindContext())
      : null;
    return schemaRows(schema);
  }
  filter(rows: ReturnType<OutputPane["rows"]>) {
    return filterRows(rows, this.query());
  }
  json(value: Json | undefined): string {
    return JSON.stringify(value, null, 2);
  }
  setView(view: DataView) {
    this.chosenView.set(view);
    writeView("output", view);
  }
  toggleSearch() {
    if (this.searching()) return this.closeSearch();
    this.focusSearch();
  }
  focusSearch() {
    this.searching.set(true);
    const session = this.session();
    setTimeout(() => {
      if (
        !this.lifetime.destroyed &&
        this.session() === session &&
        session.isCurrent() &&
        this.element.nativeElement.getClientRects().length
      )
        this.element.nativeElement
          .querySelector<HTMLInputElement>(".pane-search")
          ?.focus();
    });
  }
  closeSearch(event?: Event) {
    event?.preventDefault();
    event?.stopPropagation();
    this.searching.set(false);
    this.query.set("");
    this.element.nativeElement
      .querySelector<HTMLElement>(".pane-search-toggle")
      ?.focus();
  }
}
