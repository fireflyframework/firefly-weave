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
// The Input pane: what the step can read. The source selector lists the
// earlier steps the step can see, nearest first, then the workflow input;
// Schema shows that source's fields (draggable), with the other sources as
// "Also available". Table and JSON wait for data.
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
import {
  escapeSegment,
  referenceScope,
  WORKFLOW,
} from "../../../forms/core/scope";
import { DataTree } from "./data-tree";
import {
  countLabel,
  defaultSource,
  filterRows,
  inputSources,
  radioKey,
  savedView,
  writeView,
  sourceRows,
  type DataView,
  type TreeRow,
} from "./views";
import type { FormSession } from "../params/form-session";
import { ndvRegistry, type Json } from "../registry";

@Component({
  selector: "weave-input-pane",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, DataTree],
  template: `@let sources = this.sources();
    @let chosen = selectedSource();
    @let all = rows(chosen);
    @let shown = filter(all);
    <div class="pane-tools">
      <div
        class="pane-views"
        role="radiogroup"
        aria-label="Input view"
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
        aria-label="Search fields"
        [attr.aria-expanded]="searching()"
        (click)="toggleSearch()"
      >
        <weave-icon name="search" [size]="16" />
      </button>
    </div>
    @if (searching()) {
      <input
        class="param-input pane-search"
        aria-label="Search fields"
        placeholder="Search fields"
        [value]="query()"
        (input)="query.set($any($event.target).value)"
        (keydown.escape)="closeSearch($event)"
      />
    }
    <div class="pane-sub">
      @if (sources.length) {
        <label class="sr-only" [for]="selectId">Input source</label>
        <select
          class="pane-source"
          [id]="selectId"
          (change)="source.set($any($event.target).value)"
        >
          @for (option of sources; track option.id) {
            <option [value]="option.id" [selected]="option.id === chosen">
              {{ option.label }}{{ option.detail ? " · " + option.detail : "" }}
            </option>
          }
        </select>
      }
      <span class="pane-count">{{
        countLabel(all.length, query() ? shown.length : undefined)
      }}</span>
    </div>
    <div class="pane-content">
      @if (!sources.length) {
        <p class="pane-empty-text">This step doesn't read data.</p>
      } @else if (value() !== undefined && view() !== "schema") {
        @if (view() === "table") {
          <p class="pane-empty-text">
            Table view isn't available for this sample yet.
          </p>
          <button type="button" (click)="setView('json')">View JSON</button>
        } @else {
          <pre class="pane-json" tabindex="0" aria-label="Input data">{{
            json(value())
          }}</pre>
        }
      } @else {
        <div class="pane-empty">
          <span class="pane-empty-icon" aria-hidden="true"
            ><weave-icon name="play" [size]="20"
          /></span>
          <h4>No input data yet</h4>
          <p>
            Fields below show what this step can read. Executing previous steps
            isn’t available yet.
          </p>
        </div>
        @if (view() === "schema") {
          <h4 class="pane-caption">Fields this step can read</h4>
          <weave-data-tree
            [rows]="shown"
            label="Fields this step can read"
            [mapped]="mapped()"
          />
          @if (sources.length > 1) {
            <h4 class="pane-caption">Also available</h4>
            <div class="pane-also">
              @for (option of sources; track option.id) {
                @if (option.id !== chosen) {
                  <button
                    type="button"
                    class="pane-also-source"
                    (click)="source.set(option.id)"
                  >
                    <weave-icon [name]="option.icon" [size]="16" />{{
                      option.label
                    }}
                    @if (option.detail) {
                      <small> · {{ option.detail }}</small>
                    }
                    <weave-icon name="chevron" [size]="16" />
                  </button>
                }
              }
            </div>
          }
        }
      }
    </div>`,
})
export class InputPane {
  session = input.required<FormSession>();
  /** The data the step received, once test data or a simulation provides it. */
  value = input<Json | undefined>(undefined);
  /** References already mapped in this step. */
  mapped = input<string[]>([]);
  readonly source = signal<string | null>(null);
  readonly view = signal<DataView>(savedView("input") ?? "schema");
  readonly searching = signal(false);
  readonly query = signal("");
  readonly selectId = `input-source-${Math.random().toString(36).slice(2, 8)}`;
  readonly countLabel = countLabel;
  readonly defaultSource = defaultSource;
  readonly views: { id: DataView; label: string; icon: string }[] = [
    { id: "schema", label: "Schema", icon: "schemaView" },
    { id: "table", label: "Table", icon: "tableView" },
    { id: "json", label: "JSON", icon: "jsonView" },
  ];
  readonly radioKey = radioKey;
  private readonly lifetime = inject(DestroyRef);
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);

  private scope() {
    const session = this.session();
    const target = session.target;
    if (target === "$end")
      return referenceScope(
        session.host.model.definition,
        WORKFLOW,
        "/spec/output",
        session.controller.scopeOptions(),
      );
    const step = session.controller.step(target);
    const field = step
      ? ndvRegistry.kind(step.kind)?.fields(step)[0]
      : undefined;
    if (!field)
      return {
        found: false,
        evaluated: false,
        steps: [],
        entries: [],
        truncated: false,
      };
    const pointer =
      "/" +
      field.path.map((segment) => escapeSegment(String(segment))).join("/");
    return referenceScope(
      session.host.model.definition,
      target,
      pointer,
      session.controller.scopeOptions(),
    );
  }
  private selection: {
    session: FormSession;
    requested: string | null;
    chosen: string;
  } | null = null;
  selectedSource(): string {
    const session = this.session();
    const sources = this.sources();
    const requested = this.source();
    const changed = this.selection?.session !== session;
    const desired = changed
      ? null
      : requested !== this.selection?.requested
        ? requested
        : this.selection?.chosen;
    const chosen = sources.some((source) => source.id === desired)
      ? desired!
      : defaultSource(sources);
    this.selection = { session, requested, chosen };
    return chosen;
  }
  sources() {
    const scope = this.scope();
    return scope.found ? inputSources(scope) : [];
  }
  rows(source: string): TreeRow[] {
    return sourceRows(this.scope().entries, source);
  }
  filter(rows: TreeRow[]): TreeRow[] {
    return filterRows(rows, this.query());
  }
  json(value: Json | undefined): string {
    return JSON.stringify(value, null, 2);
  }
  setView(view: DataView) {
    this.view.set(view);
    writeView("input", view);
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
