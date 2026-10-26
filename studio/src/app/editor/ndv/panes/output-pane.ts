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
// step returns: its pinned test output (Schema with values, and JSON) until
// it runs. A step whose output can be pinned has Pin output (P) in the
// header, and without a pin offers Set test output and Generate sample.
// Steps with no output say why; steps that compute their output from their
// input say to pin the steps they read instead.
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  inject,
  input,
  effect,
  signal,
  untracked,
  viewChild,
} from "@angular/core";
import { Icon } from "../../../icon";
import { DataTree } from "./data-tree";
import { PinnedBanner } from "./pinned-banner";
import { OutputEditor } from "./output-editor";
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
  withSamples,
  type DataView,
} from "./views";
import type { FormSession } from "../params/form-session";
import { CANT_SAMPLE } from "../step-details-controller";
import { ndvRegistry, type Json } from "../registry";

@Component({
  selector: "weave-output-pane",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, DataTree, PinnedBanner, OutputEditor],
  template: `@let none = noOutput();
    @let pin = pinned();
    @let canPin = pinnable();
    @let rows = this.rows();
    @let shown = filter(rows);
    <header class="sd-pane-head">
      <h3 class="sd-pane-title">Output</h3>
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
    </header>
    @if (searching()) {
      <div class="pane-search-row">
        <input
          class="param-input pane-search"
          aria-label="Search output"
          placeholder="Search output"
          [value]="query()"
          (input)="query.set($any($event.target).value)"
          (keydown.escape)="closeSearch($event)"
        />
      </div>
    }
    @let ran = simulated();
    <div class="pane-sub">
      @if (pin) {
        <span class="pill" data-tone="info">Pinned</span>
      } @else if (ran) {
        <span class="pill pane-source-tag">Simulated</span>
      } @else if (event() !== undefined && value() === undefined) {
        <span class="pill" data-tone="info">Sample</span>
      }
      <span class="pane-count">{{
        none
          ? ""
          : event() !== undefined && view() !== "schema"
            ? sampleCount(event())
            : countLabel(rows.length, query() ? shown.length : undefined)
      }}</span>
      @if (canPin) {
        <button
          type="button"
          class="icon-button pane-pin"
          [attr.aria-pressed]="!!pin"
          [attr.aria-label]="pin ? 'Unpin output' : 'Pin output'"
          [attr.aria-disabled]="pin ? null : 'true'"
          [title]="pin ? 'Pin output (P)' : noPinYet"
          (click)="togglePin()"
        >
          <weave-icon [name]="pin ? 'unpin' : 'pin'" [size]="16" />
        </button>
        <button
          type="button"
          class="icon-button pane-edit"
          aria-label="Edit output"
          title="Edit output"
          [attr.aria-pressed]="editing()"
          (click)="setOutput()"
        >
          <weave-icon name="edit" [size]="16" />
        </button>
      }
    </div>
    <div class="sd-pane-body">
      <div class="pane-content" [class.is-editing]="editing()">
        @if (editing()) {
          @defer (on immediate) {
            <weave-output-editor
              #editor
              [session]="session()"
              [initial]="editText()"
              (saved)="saved()"
              (cancelled)="closeEditor()"
            />
          } @placeholder {
            <p class="pane-empty-text">Opening the editor…</p>
          }
        } @else {
          @if (pin) {
            <weave-pinned-banner
              [stale]="pin.stale !== null"
              [reason]="pinFromElsewhere() ? 'elsewhere' : pin.stale"
              [localOnly]="localOnly()"
              (unpin)="unpin()"
              (keep)="keepPinned()"
            />
          }
          @if (problem()) {
            <p class="pane-problem" role="alert">{{ problem() }}</p>
          }
          @if (notice(); as said) {
            <div class="pane-notice" role="status">
              <p class="pane-notice-text">{{ said.message }}</p>
              @if (said.reason === "missing_mock" && canPin && !pin) {
                <div class="pane-empty-actions">
                  <button type="button" class="sm" (click)="setOutput()">
                    Set test output
                  </button>
                  <button type="button" class="sm" (click)="generate()">
                    <weave-icon name="refresh" [size]="16" />Generate sample
                  </button>
                </div>
              }
            </div>
          }
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
            @if (computed()) {
              <p class="pane-empty-text">
                Output is computed from the input; pin the steps it reads
                instead.
              </p>
            }
            @if (canPin && !pin) {
              <div class="pane-empty">
                <span class="pane-empty-icon" aria-hidden="true"
                  ><weave-icon name="pin" [size]="20"
                /></span>
                <h4>No output yet</h4>
                <p>
                  Set test output, or generate a sample from the output fields.
                </p>
                <div class="pane-empty-actions">
                  <button type="button" class="sm" (click)="setOutput()">
                    Set test output
                  </button>
                  <button type="button" class="sm" (click)="generate()">
                    <weave-icon name="refresh" [size]="16" />Generate sample
                  </button>
                </div>
              </div>
            }
            @if (rows.length) {
              <weave-data-tree [rows]="shown" label="Output fields" />
            } @else {
              <p class="pane-empty-text">
                This step's output can be any value.
              </p>
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
                Schema shows the fields later steps can read. Executing this
                step isn’t available yet.
              </p>
            </div>
          }
        }
      </div>
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
  constructor() {
    // Another step opened in step details: an open edit belonged to the previous one.
    effect(() => {
      this.session();
      untracked(() => this.editing.set(false));
    });
  }
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);

  noOutput(): string | null {
    const step = this.session().controller.step(this.session().target);
    return step ? noOutputSentence(step) : null;
  }
  /** The step's data: its output once known, its pin or its last result, and the trigger's test event. */
  event(): Json | undefined {
    if (this.value() !== undefined) return this.value();
    const { controller, target } = this.session();
    return target === "$trigger"
      ? controller.testEvent()
      : controller.stepData(target)?.value;
  }
  /** What the step returned in the last execution, when no pin stands for it. */
  simulated(): boolean {
    const { controller, target } = this.session();
    return (
      !target.startsWith("$") &&
      controller.stepData(target)?.source === "simulated"
    );
  }
  /** What the last execution said about this step (it needs test data, a path it didn't take…). */
  notice() {
    const { controller, target } = this.session();
    const notice = controller.executeNotice();
    return notice?.target === target ? notice : null;
  }
  readonly noPinYet = "Set test output or generate a sample to pin it.";
  readonly problem = signal("");
  pinned() {
    const { controller, target } = this.session();
    return target.startsWith("$") ? null : controller.pinOfTarget(target);
  }
  pinnable(): boolean {
    const { controller, target } = this.session();
    return !target.startsWith("$") && controller.pinnable(target);
  }
  /** Kinds whose output comes from their input (transform, decision, parallel…): nothing to pin. */
  computed(): boolean {
    const { controller, target } = this.session();
    const step = controller.step(target);
    return (
      !!step && !this.pinnable() && !this.noOutput() && !target.startsWith("$")
    );
  }
  /** The pin came from another workflow and isn't kept here yet. */
  pinFromElsewhere(): boolean {
    const { controller, target } = this.session();
    return controller.pinFromElsewhere(target);
  }
  localOnly(): boolean {
    const { controller, target } = this.session();
    const key = target.replace(/~/g, "~0").replace(/\//g, "~1");
    return controller.testData.localOnly().has(`/pins/${key}`);
  }
  togglePin() {
    if (!this.pinned()) {
      this.problem.set(this.noPinYet);
      return;
    }
    this.unpin();
  }
  unpin() {
    this.problem.set("");
    this.session().controller.unpin(this.session().target);
    this.focusPin();
  }
  keepPinned() {
    this.session().controller.keepPinned(this.session().target);
    this.focusPin();
  }
  /**
   * The control that started it may go (Generate sample once pinned, the
   * banner's links as it changes): focus stays in step details, on the pin
   * toggle, which stays in place.
   */
  private focusPin() {
    setTimeout(() => {
      if (this.lifetime.destroyed) return;
      this.element.nativeElement
        .querySelector<HTMLElement>(".pane-pin")
        ?.focus({ preventScroll: true });
    });
  }
  async generate() {
    const session = this.session();
    let refused: string | null;
    try {
      refused = await session.controller.generateOutput(session.target);
    } catch {
      // Never silent: a sample that couldn't be made or kept says so.
      refused = CANT_SAMPLE;
    }
    if (this.session() !== session) return;
    this.problem.set(refused ?? "");
    if (!refused) {
      session.announce(`Pinned a sample output for ${session.target}.`);
      this.focusPin();
    }
  }
  /** Set test output and Edit output: the JSON editor, with the pinned value or {}. */
  setOutput() {
    if (this.editing()) return this.editor()?.focus();
    const pinned = this.event();
    this.editText.set(
      pinned === undefined ? "{}" : JSON.stringify(pinned, null, 2),
    );
    this.problem.set("");
    this.editing.set(true);
  }
  readonly editing = signal(false);
  readonly editText = signal("{}");
  /**
   * The open editor, queried by name: naming its class here would load it,
   * and CodeMirror with it, with step details instead of on first use.
   */
  private readonly editor = viewChild<{
    dirty(): boolean;
    save(): Promise<boolean>;
    focus(): void;
  }>("editor");
  /** An output edit not saved yet: closing asks first. */
  editDirty(): boolean {
    return this.editing() && !!this.editor()?.dirty();
  }
  /** Saves the open edit (the guard's Save); true once pinned. */
  async saveEdit(): Promise<boolean> {
    const editor = this.editor();
    return editor ? editor.save() : true;
  }
  focusEditor() {
    this.editor()?.focus();
  }
  saved() {
    const session = this.session();
    this.editing.set(false);
    session.announce(`Pinned the output of ${session.target}.`);
    this.focusEdit();
  }
  closeEditor() {
    this.editing.set(false);
    this.focusEdit();
  }
  private focusEdit() {
    setTimeout(() =>
      this.element.nativeElement
        .querySelector<HTMLElement>(".pane-edit")
        ?.focus({ preventScroll: true }),
    );
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
    // With pinned output, each field shows its value.
    return withSamples(schemaRows(schema), this.event(), "");
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
