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
// "Also available". With data (the test input, an earlier step's pin or last
// result) Schema shows each field's value, Table its rows and JSON the data.
// Compact (Edit formula's
// column): the fields to drag, with the search and the source on one row,
// and no header or empty-state text.
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  inject,
  input,
  signal,
  type OnInit,
} from "@angular/core";
import { Icon } from "../../../icon";
import {
  escapeSegment,
  referenceScope,
  WORKFLOW,
} from "../../../forms/core/scope";
import { DataTree } from "./data-tree";
import { enclosingLoops, stepPointerOf } from "../../formula/loops";
import {
  IterationPicker,
  pickerLoops,
  type PickerLoop,
} from "./iteration-picker";
import { DataTable } from "./data-table";
import { tableCount } from "./table-view";
import {
  countLabel,
  defaultSource,
  filterRows,
  inputSources,
  loopSources,
  radioKey,
  rootOf,
  savedView,
  withSamples,
  writeView,
  sourceRows,
  type DataView,
  type PaneEntry,
  type TreeRow,
} from "./views";
import type { FormSession } from "../params/form-session";
import { ndvRegistry, type Json } from "../registry";
import { instanceStep } from "../../state/instance-keys";

@Component({
  selector: "weave-input-pane",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, DataTree, DataTable, IterationPicker],
  host: { "[class.is-compact]": "compact()" },
  template: `@let sources = this.sources();
    @let chosen = selectedSource();
    @let data = dataOf(chosen);
    @let all = rows(chosen, data);
    @let shown = filter(all);
    @if (!compact()) {
      <header class="sd-pane-head">
        <h3 class="sd-pane-title">Input</h3>
        <div
          class="pane-views"
          role="radiogroup"
          aria-label="Input view"
          (keydown)="radioKey($event)"
        >
          @let shownAs =
            selectedSource() === "received" ? receivedView() : view();
          @for (choice of views; track choice.id) {
            @let noFields =
              selectedSource() === "received" && choice.id === "schema";
            <button
              type="button"
              role="radio"
              [attr.aria-checked]="shownAs === choice.id"
              [attr.tabindex]="shownAs === choice.id ? 0 : -1"
              [attr.aria-disabled]="noFields ? 'true' : null"
              [attr.title]="
                noFields ? 'What a step received has no fields to list' : null
              "
              (click)="!noFields && setView(choice.id)"
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
      </header>
    }
    @if (searching() || compact()) {
      <div class="pane-search-row">
        <input
          class="param-input pane-search"
          [attr.aria-label]="searchLabel()"
          [placeholder]="searchLabel()"
          [value]="query()"
          (input)="query.set($any($event.target).value)"
          (keydown.escape)="searchEscape($event)"
        />
      </div>
    }
    <div class="pane-sub">
      @if (sources.length) {
        <label class="sr-only" [for]="selectId">Input source</label>
        <span class="pane-source-field">
          <select
            class="pane-source"
            [id]="selectId"
            (change)="source.set($any($event.target).value)"
          >
            @for (option of sources; track option.id) {
              <option [value]="option.id" [selected]="option.id === chosen">
                {{ option.label
                }}{{ option.detail ? " · " + option.detail : "" }}
              </option>
            }
          </select>
          <weave-icon
            class="pane-source-chevron"
            name="chevronDown"
            [size]="16"
            aria-hidden="true"
          />
        </span>
      }
      @if (data !== undefined && sources.length) {
        @let tag = sourceTag(chosen);
        <span
          class="pill pane-source-tag"
          [attr.data-tone]="tag === 'Simulated' ? null : 'info'"
          >{{ tag }}</span
        >
      }
      <span class="pane-count">{{
        chosen === "received"
          ? ""
          : data !== undefined && shownView() === "table"
            ? tableCount(data, query())
            : countLabel(all.length, query() ? shown.length : undefined)
      }}</span>
    </div>
    @if (!compact()) {
      @let around = pickerRows();
      @if (around.length) {
        <weave-iteration-picker [loops]="around" (picked)="pick($event)" />
      }
    }
    <div class="sd-pane-body">
      <div class="pane-content">
        @if (chosen === "received") {
          @let got = received();
          @if (got.ok) {
            @if (receivedView() === "table") {
              <weave-data-table
                [value]="got.value"
                label="What it received"
                [query]="query()"
              />
            } @else {
              <pre
                class="pane-json pane-received"
                tabindex="0"
                aria-label="What it received"
                >{{ json(got.value) }}</pre
              >
            }
            <p class="pane-received-note">
              Studio evaluated {{ got.step }}'s input mapping with this run's
              data{{ got.version ? " from version " + got.version : "" }}, so
              this is what the step received.
            </p>
          } @else if (got.reason) {
            <p class="pane-empty-text">{{ got.reason }}</p>
          } @else {
            <p class="pane-empty-text">This step's input maps nothing.</p>
          }
        } @else if (!sources.length) {
          <p class="pane-empty-text">This step doesn't read data.</p>
        } @else if (data !== undefined && shownView() !== "schema") {
          @if (shownView() === "table") {
            <weave-data-table
              [value]="data"
              label="Input data"
              [query]="query()"
            />
          } @else {
            <pre class="pane-json" tabindex="0" aria-label="Input data">{{
              json(data)
            }}</pre>
          }
        } @else {
          @if (!compact() && session().controller.running()) {
            <div class="pane-empty" role="status">
              <span class="loading-spinner small" aria-hidden="true"></span>
              <h4>Executing previous steps…</h4>
              <div class="pane-empty-actions">
                <button type="button" class="sm" (click)="stop()">
                  <weave-icon name="close" [size]="16" />Stop
                </button>
              </div>
            </div>
          } @else if (!compact() && data === undefined) {
            @let missing = notRun(chosen);
            <div class="pane-empty">
              <span class="pane-empty-icon" aria-hidden="true"
                ><weave-icon name="play" [size]="20"
              /></span>
              @if (missing) {
                <p>
                  {{ missing }} hasn't run yet. Execute previous steps, or map
                  from the fields below.
                </p>
              } @else {
                <h4>No input data yet</h4>
                <p>
                  Execute the previous steps to see what this step receives, or
                  add test input.
                </p>
              }
              <div class="pane-empty-actions">
                @if (session().controller.canExecute()) {
                  <button
                    type="button"
                    class="sm"
                    title="Runs from the trigger up to this step, reusing pinned and current results"
                    (click)="executePrevious()"
                  >
                    <weave-icon name="play" [size]="16" />Execute previous steps
                  </button>
                }
                @if (!missing) {
                  <button type="button" class="sm" (click)="addTestInput()">
                    Add test input
                  </button>
                }
              </div>
            </div>
          } @else if (!compact() && staleSource(chosen)) {
            <div class="pinned-banner pane-stale" data-tone="warning">
              <weave-icon name="stale" [size]="16" aria-hidden="true" />
              <p>
                <span role="status"
                  >Earlier steps changed since this data was produced.</span
                >&ngsp;
                @if (session().controller.canExecute()) {
                  <button
                    type="button"
                    class="text-link"
                    (click)="executePrevious()"
                  >
                    Execute previous steps
                  </button>
                }
              </p>
            </div>
          }
          @if (shownView() === "schema") {
            @if (!compact()) {
              <h4 class="pane-caption">Fields this step can read</h4>
            }
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
      </div>
    </div>`,
})
export class InputPane implements OnInit {
  session = input.required<FormSession>();
  /** The data the step received, once test data or a simulation provides it. */
  value = input<Json | undefined>(undefined);
  /** References already mapped in this step. */
  mapped = input<string[]>([]);
  /** The search field's name and placeholder. */
  searchLabel = input("Search fields");
  /** Start with the search field open (the Edit formula dialog). */
  openSearch = input(false);
  /** Edit formula's column: fields only, the search always shown. */
  compact = input(false);
  /** The Edit formula dialog opens with the search field shown. */
  ngOnInit() {
    if (this.openSearch()) this.searching.set(true);
    // Where Debug in editor stopped: what the step was given, as data.
    const { host, target } = this.session();
    const notice = host.runNotice;
    if (
      !this.compact() &&
      notice &&
      notice.draftId === host.draftId &&
      instanceStep(notice.key) === instanceStep(target)
    )
      this.view.set("json");
  }
  readonly source = signal<string | null>(null);
  readonly view = signal<DataView>(savedView("input") ?? "schema");
  readonly searching = signal(false);
  readonly query = signal("");
  readonly selectId = `input-source-${Math.random().toString(36).slice(2, 8)}`;
  readonly countLabel = countLabel;
  readonly tableCount = tableCount;
  readonly defaultSource = defaultSource;
  readonly views: { id: DataView; label: string; icon: string }[] = [
    { id: "schema", label: "Schema", icon: "schemaView" },
    { id: "table", label: "Table", icon: "tableView" },
    { id: "json", label: "JSON", icon: "jsonView" },
  ];
  readonly radioKey = radioKey;
  private readonly lifetime = inject(DestroyRef);
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  /** Counts asked of the current data, by loop, until the data changes. */
  private asked = new Set<string>();
  private askedFor: readonly unknown[] = [];

  /** A loop's count the last Execute didn't give: asked of the current data, once until that data changes. */
  private countLoop(loopId: string) {
    const { controller } = this.session();
    if (controller.viewingRuns()) return;
    const stamp = controller.previewData().stamp();
    if (
      stamp.length !== this.askedFor.length ||
      stamp.some((part, i) => part !== this.askedFor[i])
    ) {
      this.asked = new Set();
      this.askedFor = stamp;
    }
    const iterations = controller.iterations;
    if (iterations.counts(loopId).known || this.asked.has(loopId)) return;
    this.asked.add(loopId);
    void iterations.evaluate(loopId);
  }

  /** One picker row per loop around the step, innermost last; none outside loops or on the Runs tab. */
  pickerRows(): PickerLoop[] {
    const { controller, target } = this.session();
    if (controller.viewingRuns() || target.startsWith("$")) return [];
    const iterations = controller.iterations;
    const rows = pickerLoops(
      iterations.picks(target),
      (loopId) => iterations.counts(loopId),
      (loopId) => {
        const limit = controller.step(loopId)?.["maxItems"];
        return typeof limit === "number" && limit > 0 ? limit : 1000;
      },
    );
    // Outside the render: counting writes signals when its answer arrives.
    for (const row of rows)
      if (!row.known) queueMicrotask(() => this.countLoop(row.loopId));
    return rows;
  }
  pick({ loopId, index }: { loopId: string; index: number }) {
    this.session().controller.iterations.pick(loopId, index);
  }
  /** What a field inside a loop body can read, from the host, with the loops' items; null elsewhere or while asked. */
  private hosted(pointer: string): PaneEntry[] | null {
    const { host, controller, target } = this.session();
    const definition = host.model.definition;
    if (target.startsWith("$") || !enclosingLoops(definition, target).length)
      return null;
    const step = stepPointerOf(definition, target);
    if (!step) return null;
    return controller.loopScopes.entries(
      definition,
      `${step}${pointer}`,
    ) as unknown as PaneEntry[] | null;
  }

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
    const hosted = this.hosted(pointer);
    if (hosted)
      return {
        found: true,
        evaluated: true,
        loops: true,
        steps: hosted
          .filter((entry) => entry.source === "step" && !entry.path.length)
          .map((entry) => {
            const id = entry.ref
              .split("/")[2]
              .replace(/~1/g, "/")
              .replace(/~0/g, "~");
            return { id, kind: session.controller.step(id)?.kind ?? "action" };
          }),
        entries: hosted,
        truncated: false,
      };
    return referenceScope(
      session.host.model.definition,
      target,
      pointer,
      session.controller.scopeOptions(),
    );
  }
  private selection: {
    session: FormSession;
    /** The run open in run mode: another run starts at What it received. */
    run: string | null;
    requested: string | null;
    chosen: string;
    /** Chosen by default before the person chose any source: it follows the sources as they change. */
    defaulted: boolean;
  } | null = null;
  selectedSource(): string {
    const session = this.session();
    const sources = this.sources();
    const requested = this.source();
    const run = session.controller.run()?.runId ?? null;
    const changed =
      this.selection?.session !== session || this.selection?.run !== run;
    const desired = changed
      ? null
      : requested !== this.selection?.requested
        ? requested
        : this.selection?.defaulted
          ? null
          : this.selection?.chosen;
    const kept = sources.some((source) => source.id === desired);
    const chosen = kept ? desired! : defaultSource(sources);
    // Until the person chooses a source, the default follows the sources
    // (a loop body's items arrive from the host after the step's own).
    this.selection = {
      session,
      run,
      requested,
      chosen,
      defaulted: !kept && requested === null,
    };
    return chosen;
  }
  sources() {
    const scope = this.scope();
    const sources = !scope.found
      ? []
      : "loops" in scope
        ? loopSources(scope.entries, scope.steps)
        : inputSources(scope);
    // In run mode the first source is what the step received in that run.
    const { controller, target } = this.session();
    return controller.run() && !target.startsWith("$") && !this.compact()
      ? [
          {
            id: "received",
            label: "What it received",
            detail: "",
            icon: "output",
          },
          ...sources,
        ]
      : sources;
  }
  /** What it received shows as Table or JSON: it has no fields to list as Schema. */
  receivedView(): DataView {
    const view = this.view();
    return view === "schema" ? "json" : view;
  }
  /** What the step received in the run, recomputed, or why Studio can't say. */
  received() {
    const { controller, target } = this.session();
    const received = controller.runReceived(target);
    const version = controller.run()?.definitionVersion() ?? null;
    return received.ok
      ? {
          version,
          ok: true as const,
          value: received.value as Json,
          reason: "",
          step: target,
        }
      : {
          version,
          ok: false as const,
          value: undefined,
          reason: received.reason,
          step: target,
        };
  }
  /** The chosen source's data: the test input, an earlier step's pinned output, or what the step received. */
  dataOf(source: string): Json | undefined {
    const controller = this.session().controller;
    if (source === "received") {
      const received = controller.runReceived(this.session().target);
      return received.ok ? (received.value as Json) : undefined;
    }
    // On the Runs tab only a run's data shows, never the draft's sample.
    if (source === "input" && controller.viewingRuns())
      return controller.testEvent();
    if (source === "input")
      return (
        controller.testEvent() ??
        this.session().host.execution?.sampleUsed() ??
        this.value()
      );
    if (source.startsWith("step:"))
      return controller.stepData(source.slice(5))?.value ?? this.value();
    return this.value();
  }
  /** The earlier step chosen as the source, when it has no data yet (it hasn't run). */
  notRun(source: string): string | null {
    if (!source.startsWith("step:")) return null;
    const id = source.slice(5);
    return this.session().controller.stepData(id) ? null : id;
  }
  /** The chosen source's last result is out of date. */
  staleSource(source: string): boolean {
    if (!source.startsWith("step:")) return false;
    const data = this.session().controller.stepData(source.slice(5));
    return data?.source === "simulated" && data.stale !== null;
  }
  executePrevious() {
    this.session().controller.requestExecute("before");
  }
  stop() {
    this.session().host.execution?.stop();
  }
  /** Add test input: the trigger's Test event. */
  addTestInput() {
    void this.session().host.openStepDetails("$trigger");
  }
  /** Where the chosen source's data comes from: test input, a pin, or the last execution. */
  sourceTag(source: string): string {
    const run = this.session().controller.run();
    if (source === "received") return "Recomputed from the run's data";
    if (run) return `From ${run.label()}`;
    if (source === "input")
      return this.session().controller.testEvent() === undefined &&
        this.session().host.execution?.sampleUsed() != null
        ? "Sample"
        : "Test input";
    const data = source.startsWith("step:")
      ? this.session().controller.stepData(source.slice(5))
      : undefined;
    return data?.source === "simulated" ? "Simulated" : "Pinned";
  }
  rows(source: string, data?: Json): TreeRow[] {
    return withSamples(
      sourceRows(this.scope().entries, source),
      data,
      rootOf(source),
    );
  }
  filter(rows: TreeRow[]): TreeRow[] {
    return filterRows(rows, this.query());
  }
  json(value: Json | undefined): string {
    return JSON.stringify(value, null, 2);
  }
  /** Compact shows the fields to drag, whatever view the pane last had. */
  shownView(): DataView {
    return this.compact() ? "schema" : this.view();
  }
  /** Escape in the search: clears it; compact keeps it shown and lets an empty one pass Escape on. */
  searchEscape(event: Event) {
    if (!this.compact()) return this.closeSearch(event);
    if (!this.query()) return;
    event.preventDefault();
    event.stopPropagation();
    this.query.set("");
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
