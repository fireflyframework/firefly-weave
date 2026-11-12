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
// The decision table grid, in table view of step details. It edits the
// step's table as a spreadsheet: one row per rule, a cell per input column
// (a short test such as "> 1000") and per result column, and an Otherwise
// row. Editing a published version starts the draft of its next version on
// this computer and moves the step to it in the same undo step; every change
// after that is kept in the draft a moment later. The Input and Output panes
// try the table as the person types: Input holds the test values, Output the
// rule that matched and the result, and each rule's mark shows in the grid.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  type DoCheck,
  DestroyRef,
  ElementRef,
  Injector,
  afterEveryRender,
  afterNextRender,
  computed,
  effect,
  inject,
  isDevMode,
  untracked,
  input,
  signal,
} from "@angular/core";
import { Icon } from "../../icon";
import { Select, type SelectOption } from "../../forms/ui/select";
import { RowMenu, type RowMenuItem } from "../../row-menu";
import { printFormula } from "../formula/printer";
import type {
  Expression,
  Json,
  NdvContext,
  ParameterComponent,
  ParameterPanes,
} from "../ndv/registry";
import {
  COLUMN_TYPE_LABELS,
  parseCell,
  printCell,
  type ColumnType,
} from "./cell-grammar";
import {
  COLUMN_TYPE_ICONS,
  ColumnDialog,
  type ColumnAnswer,
  type ColumnRequest,
} from "./column-dialog";
import { gridProblems, type GridProblem } from "./grid-checks";
import { FormulaDialog } from "./formula-dialog";
import { TryInputService, type TryRequest } from "./try-input-service";
import { TableInputPane, TableOutputPane } from "./try-panes";
import { TableTry } from "./table-try";
import { tryView, type GridMark, type TryView } from "./try-view";
import { formulaParts, splitIntoCells, tableFormula } from "./formula-cells";
import {
  GridHistory,
  moveCursor,
  parsePaste,
  pasteInto,
  type CursorCommand,
} from "./grid-keys";
import { commandFor, eventCombo, KEY_PLATFORM } from "../state/keymap";
import { parseResult, printResult } from "./result-cells";
import {
  addInputColumn,
  addResultColumn,
  addRule,
  composeWhen,
  decomposeWhen,
  duplicateRule,
  gridOf,
  moveRule,
  removeInputColumn,
  removeResultColumn,
  removeRule,
  renameInputColumn,
  renameResultColumn,
  renameRule,
  setCondition,
  setHitPolicy,
  setInputColumnType,
  setResult,
  setRuleFormula,
  type GridColumn,
  type GridView,
  type HitPolicy,
  type TableSpec,
} from "./rules-model";

/** A column of the grid: the rule's name, an input or a result. */
export type GridCol =
  | { kind: "name" }
  | { kind: "input"; index: number; column: GridColumn }
  | { kind: "result"; index: number; column: GridColumn };
export interface GridCellAt {
  row: number;
  col: number;
}

export const POLICY_OPTIONS: SelectOption[] = [
  { value: "first", label: "First match" },
  { value: "unique", label: "Exactly one" },
  { value: "collect", label: "Collect all" },
];
export const POLICY_HINTS: Record<HitPolicy, string> = {
  first: "The first match wins. Put more specific rules before broader rules.",
  unique: "The run stops for review if more than one rule matches.",
  collect:
    "All matching results become a list. No matches returns an empty list.",
};
const SAVE_DELAY = 300;

const nameOf = (uses: string) => uses.slice(0, uses.lastIndexOf("@"));
const versionOf = (uses: string) => uses.slice(uses.lastIndexOf("@") + 1);
const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const specOf = (document: Json | null): TableSpec | null =>
  isRecord(document) && isRecord(document["spec"])
    ? (document["spec"] as unknown as TableSpec)
    : null;

@Component({
  selector: "weave-decision-grid",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, Select, RowMenu, ColumnDialog, FormulaDialog],
  templateUrl: "./grid.html",
  styleUrl: "./grid.css",
})
export class DecisionGrid implements ParameterComponent, DoCheck {
  readonly context = input.required<NdvContext>();
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);
  private readonly lifetime = inject(DestroyRef);
  private readonly changes = inject(ChangeDetectorRef);

  /** The table the grid shows and edits. */
  readonly spec = signal<TableSpec | null>(null);
  readonly view = computed<GridView | null>(() => {
    const spec = this.spec();
    return spec ? gridOf(spec) : null;
  });
  readonly cols = computed<GridCol[]>(() => {
    const view = this.view();
    if (!view) return [];
    return [
      { kind: "name" },
      ...view.inputs.map((column, index) => ({
        kind: "input" as const,
        index,
        column,
      })),
      ...view.results.map((column, index) => ({
        kind: "result" as const,
        index,
        column,
      })),
    ];
  });
  /** The rules, then Otherwise when the table has one. */
  readonly rowCount = computed(() => {
    const view = this.view();
    return view ? view.rows.length + (view.otherwise ? 1 : 0) : 0;
  });
  readonly rowIndexes = computed(() =>
    Array.from({ length: this.rowCount() }, (_, index) => index),
  );
  readonly cursor = signal<GridCellAt>({ row: 0, col: 0 });
  /** The cell being edited; it keeps the height it had, so its row doesn't move. */
  readonly editing = signal<
    | (GridCellAt & { text: string; original: string; height: number | null })
    | null
  >(null);
  /** True while the grid is scrolled sideways: the pinned columns show their edge. */
  readonly scrolledSideways = signal(false);
  /** Cell text that doesn't read, kept in its cell with its message and never written. */
  readonly flagged = signal<
    ReadonlyMap<string, { text: string; message: string }>
  >(new Map());
  readonly notice = signal<string | null>(null);
  readonly dialog = signal<ColumnRequest | null>(null);
  /** Edit formula, open on a rule's condition or one of its results. */
  readonly formula = signal<{
    row: number;
    col: number;
    heading: string;
    text: string;
  } | null>(null);
  readonly announcement = signal("");

  /** The step's `uses` the shown table belongs to. */
  private shownFor = "";
  /** The draft a first change is starting, until the step moves to it. */
  private starting: Promise<string | null> | null = null;
  private saveTimer: ReturnType<typeof setTimeout> | null = null;
  /** The account of the edits waiting to be kept. */
  private editedIn: string | null = null;
  /** Counts the table's changes: a try's answers apply to the one they were asked for. */
  readonly revision = signal(0);
  private readonly tries = new TryInputService(
    (body: TryRequest) =>
      this.context().tables?.evaluate(
        body as unknown as {
          table: Json;
          input: Json;
        },
      ) ?? Promise.reject(new TypeError("No Studio host")),
    {
      // Development builds say when Studio and Weave disagree, with codes only.
      warn: isDevMode()
        ? (message, data) => console.warn(message, data)
        : undefined,
    },
  );
  readonly tryState = this.tries.state;
  /** The grid's own undo history, apart from the workflow's. */
  private readonly history = new GridHistory();
  /** The problems under the grid, read once per change. */
  private readonly found = computed(() => this.problems());
  readonly errorCount = computed(
    () => this.found().filter((problem) => problem.severity === "error").length,
  );
  /** The rules with an error in one of their cells. */
  private readonly problemRows = computed(() => {
    const rows = this.view()?.rows.length ?? 0;
    return new Set(
      this.found()
        .filter((problem) => problem.severity === "error")
        .flatMap((problem) =>
          problem.at && problem.at.row < rows ? [problem.at.row] : [],
        ),
    );
  });
  /** The first error: its rule, "table" when it isn't a rule's, or none. */
  private readonly firstError = computed<number | "table" | null>(() => {
    const error = this.found().find((problem) => problem.severity === "error");
    if (!error) return null;
    const rows = this.view()?.rows.length ?? 0;
    return error.at && error.at.row < rows ? error.at.row : "table";
  });
  /** What the Output pane shows, and each rule's mark. */
  readonly shown = computed<TryView | null>(() => {
    const view = this.view();
    if (!view) return null;
    return tryView({
      view,
      state: this.tryState(),
      revision: this.revision(),
      empty: this.table.empty(),
      unfit: this.table.unfit(),
      problem: this.firstError(),
    });
  });
  /** Trying the table, shared with the Input and Output panes. */
  readonly table: TableTry = new TableTry({
    spec: this.spec,
    view: this.view,
    shown: this.shown,
    readOnly: computed(() => this.context().readOnly),
    goToRule: (row) => this.focusCell(row, 0),
  });
  readonly panes: ParameterPanes = {
    input: TableInputPane,
    output: TableOutputPane,
    state: this.table,
  };
  /** While a draft is open, Publish table is the one primary button. */
  readonly ownsPrimary = signal(false);
  private tryStarted = false;
  readonly formulaParts = formulaParts;

  constructor() {
    this.lifetime.onDestroy(() => {
      void this.flush();
      // What was tried is forgotten with the grid.
      this.tries.clear();
    });
    // The grid's and the problems' minimums follow what is drawn: rows grow
    // for a formula, the footer and the problems wrap on a narrow card.
    afterEveryRender(() => this.measure());
    const resized = new ResizeObserver(() => this.measure());
    resized.observe(this.element.nativeElement);
    this.lifetime.onDestroy(() => resized.disconnect());
    this.resized = resized;
    // The step's test data fills Input when the table opens.
    effect(() => {
      const context = this.context();
      if (this.tryStarted) return;
      this.tryStarted = true;
      untracked(() => {
        const sample = context.sample.resolvedInput();
        this.table.start(
          sample.state === "available" && isRecord(sample.value)
            ? (sample.value as Record<string, unknown>)
            : null,
        );
      });
    });
    // A column added, renamed or removed changes the input fields: the
    // values being tried follow it, and Input starts again from them.
    const fieldsOf = (schema: unknown) =>
      isRecord(schema) && isRecord(schema["properties"])
        ? Object.keys(schema["properties"])
        : [];
    let schema: unknown;
    effect(() => {
      const next = this.spec()?.inputSchema;
      untracked(() => {
        if (schema !== undefined && next !== schema)
          this.table.reshape(fieldsOf(schema), fieldsOf(next));
        schema = next;
      });
    });
    // Each change of the values or the table tries it again; a table with
    // problems isn't the one the grid shows, so it isn't tried. An input
    // that doesn't fit is still sent, so Weave says what doesn't fit.
    effect(() => {
      const spec = this.spec();
      const value = this.table.value();
      const revision = this.revision();
      const blocked = this.firstError() !== null || this.table.empty();
      untracked(() => {
        if (!spec || blocked) return this.tries.clear();
        this.tries.set(this.documentOf(spec), value as Json, revision);
      });
    });
  }
  /** The table as a document, named as the step uses it. */
  private documentOf(spec: TableSpec) {
    const uses = this.shownFor || this.uses();
    return {
      apiVersion: "weave/v1alpha1" as const,
      kind: "DecisionTable" as const,
      metadata: { name: nameOf(uses), version: versionOf(uses) },
      spec,
    };
  }
  /**
   * Sets the drawn minimums: the grid box holds its header, its first five
   * body rows (all of them when fewer) and its footer; the problems list
   * holds its head and its first three problems. The table bar drops its
   * divider when the table's name starts a new line.
   */
  private resized: ResizeObserver | null = null;
  private readonly observed = new WeakSet<Element>();
  private measure() {
    const root = this.element.nativeElement;
    const height = (element: Element | null | undefined) =>
      element?.getBoundingClientRect().height ?? 0;
    const set = (element: HTMLElement, name: string, px: number) => {
      const value = `${Math.ceil(px)}px`;
      if (element.style.getPropertyValue(name) !== value)
        element.style.setProperty(name, value);
    };
    const box = root.querySelector<HTMLElement>(".dg-box");
    if (box) {
      const rows = [
        ...box.querySelectorAll(".dg-row:not(.dg-header) > .dg-mark-cell"),
      ].slice(0, 5);
      set(
        box,
        "--dg-box-min",
        height(box.querySelector(".dg-group")) +
          height(box.querySelector(".dg-header > .dg-th")) +
          rows.reduce((sum, row) => sum + height(row), 0) +
          height(box.querySelector(".dg-foot")) +
          2,
      );
    }
    const problems = root.querySelector<HTMLElement>(".dg-problems");
    if (problems)
      set(
        problems,
        "--dg-problems-max",
        height(problems.querySelector(".dg-problems-head")) +
          [...problems.querySelectorAll("li")]
            .slice(0, 3)
            .reduce((sum, item) => sum + height(item), 0) +
          2,
      );
    const bar = root.querySelector<HTMLElement>(".dg-bar");
    const back = bar?.querySelector<HTMLElement>(".dg-back");
    const ident = bar?.querySelector<HTMLElement>(".dg-ident");
    if (bar && back && ident) {
      // Their widths change with no render or resize of the grid too, as
      // when a web font arrives: watch them.
      for (const element of [bar, back, ident])
        if (!this.observed.has(element)) {
          this.observed.add(element);
          this.resized?.observe(element);
        }
      // The name follows Back on its line while Back, the divider and the
      // name fit with their gaps; Publish table may wrap on its own. The
      // divider counts even while hidden, so hiding it never lets the name
      // fit again.
      const width = (element: Element) => element.getBoundingClientRect().width;
      const gap = parseFloat(getComputedStyle(bar).columnGap) || 0;
      const needed = width(back) + 1 + width(ident) + 2 * gap;
      bar.classList.toggle("is-wrapped", needed > width(bar));
    }
  }
  /** The text an Otherwise cell shows while the table has no Otherwise result. */
  placeholder(row: number, col: number): string | null {
    const view = this.view();
    return view &&
      !view.otherwiseSet &&
      this.isOtherwise(row) &&
      this.cols()[col]?.kind === "result"
      ? "No result"
      : null;
  }
  /** A rule's mark in the # column: an error in its cells first, then the try. */
  mark(row: number): GridMark | null {
    if (this.isOtherwise(row)) return this.shown()?.otherwise ?? null;
    if (this.problemRows().has(row)) return "error";
    return this.shown()?.marks[row] ?? null;
  }
  /** The row's tint: matched, or refused by Exactly one. */
  rowState(row: number): "match" | "refused" | null {
    const mark = this.mark(row);
    return mark === "match" || mark === "refused" ? mark : null;
  }
  markText(row: number, mark: GridMark): string {
    switch (mark) {
      case "match":
        return "Matches";
      case "no-match":
        return "Doesn't match";
      case "refused":
        return "Matches, as another rule does";
      default:
        return this.problemRows().has(row)
          ? "Has a problem"
          : "Can't be checked with this input";
    }
  }
  ngDoCheck() {
    this.sync();
    this.ownsPrimary.set(this.where() === "draft" && !!this.view());
  }

  // ------------------------------------------------------------- reading
  uses(): string {
    const uses = this.context().step["uses"];
    return typeof uses === "string" ? uses : "";
  }
  where(): "draft" | "copy" | "published" | null {
    return this.context().tables?.where(this.uses()) ?? null;
  }
  /** Reads the step's table again when the step moved to another one (an Undo, a choice in Table). */
  sync(): TableSpec | null {
    const uses = this.uses();
    if (uses !== this.shownFor && !this.starting) {
      this.shownFor = uses;
      const spec = specOf(this.context().tables?.document(uses) ?? null);
      this.spec.set(spec ? structuredClone(spec) : null);
      this.revision.update((revision) => revision + 1);
      this.history.clear();
      this.flagged.set(new Map());
      this.editing.set(null);
      this.notice.set(null);
    }
    return this.spec();
  }
  title(): string {
    return `${this.tableName()} ${this.tableVersion()}`;
  }
  tableName(): string {
    return nameOf(this.uses());
  }
  tableVersion(): string {
    return versionOf(this.uses());
  }
  /** The status pill beside the table's name. */
  status(): { text: string; tone: "warning" | "success" } | null {
    const where = this.where();
    if (where === "draft") return { text: "Draft", tone: "warning" };
    if (where === "published" || where === "copy")
      return { text: "Published", tone: "success" };
    return null;
  }
  /** What the status does to test runs and to publishing. */
  banner(): string | null {
    const where = this.where();
    if (where === "draft")
      return `Draft ${this.tableVersion()} is saved on this computer, and test runs use it. Publish it before publishing this workflow.`;
    if (where === "published" || where === "copy")
      return `${this.title()} is published. Your first change starts the next version on this computer.`;
    return null;
  }
  gridLabel(): string {
    const uses = this.uses();
    return `Rules of ${nameOf(uses)} ${versionOf(uses)}`;
  }
  policyHint(): string {
    return POLICY_HINTS[this.spec()?.hitPolicy ?? "first"];
  }
  readonly policyOptions = POLICY_OPTIONS;
  readOnly(): boolean {
    return this.context().readOnly;
  }
  typeLabel(column: GridColumn): string {
    return column.type ? COLUMN_TYPE_LABELS[column.type] : "Other";
  }
  typeIcon(column: GridColumn): string {
    return column.type ? COLUMN_TYPE_ICONS[column.type] : "typeAny";
  }
  /**
   * The tracks: # and Rule name, then the inputs and the results. They
   * stretch to fill the box from fixed minimums and never follow a cell's
   * text, so editing a cell moves no column.
   */
  template(): string {
    const view = this.view();
    if (!view) return "";
    // The minimums are grid.css's: compact from 768 to 1099 px.
    return [
      "var(--dg-mark) minmax(var(--dg-name), 1.2fr)",
      ...view.inputs.map(() => "minmax(var(--dg-input), 1.5fr)"),
      ...view.results.map(() => "minmax(var(--dg-result), 1fr)"),
    ].join(" ");
  }
  /** Body rows the grid's box always shows, all of them when there are fewer than five. */
  minRows(): number {
    return Math.min(5, Math.max(1, this.rowCount()));
  }
  ruleCount(): string {
    const count = this.view()?.rows.length ?? 0;
    return `${count} rule${count === 1 ? "" : "s"}`;
  }
  /** A problem's place: "Rule 1 · amount", "Rule 2 · Rule name". */
  locationOf(at: GridCellAt): string {
    const column = this.cols()[at.col];
    return `${this.rowLabel(at.row)} · ${
      !column || column.kind === "name" ? "Rule name" : column.column.name
    }`;
  }
  isOtherwise(row: number): boolean {
    return row === (this.view()?.rows.length ?? -1);
  }
  rowLabel(row: number): string {
    return this.isOtherwise(row) ? "Otherwise" : `Rule ${row + 1}`;
  }
  colName(col: GridCol): string {
    return col.kind === "name" ? "rule name" : col.column.name;
  }
  /** The cell's accessible name in edit mode: "Rule 2, amount". */
  cellLabel(row: number, col: number): string {
    const column = this.cols()[col];
    return `${this.rowLabel(row)}, ${column ? this.colName(column) : ""}`;
  }
  /** True when the rule's condition is one formula spanning the input columns. */
  merged(row: number): boolean {
    return this.view()?.rows[row]?.condition.kind === "formula";
  }
  mergedResults(row: number): boolean {
    const view = this.view();
    if (!view) return false;
    const result = this.isOtherwise(row)
      ? view.otherwise
      : view.rows[row]?.result;
    return result?.kind === "formula";
  }
  key(row: number, col: number): string {
    return `${row}:${col}`;
  }
  /** What a cell shows. */
  text(row: number, col: number): string {
    const flagged = this.flagged().get(this.key(row, col));
    if (flagged) return flagged.text;
    const view = this.view();
    const column = this.cols()[col];
    if (!view || !column) return "";
    const other = this.isOtherwise(row);
    if (column.kind === "name")
      return other ? "Otherwise" : (view.rows[row]?.id ?? "");
    if (column.kind === "input") {
      if (other) return "When no rule matches";
      const condition = view.rows[row]?.condition;
      if (!condition) return "";
      if (condition.kind === "formula")
        return printFormula(condition.when) ?? JSON.stringify(condition.when);
      const cell = condition.cells[column.index];
      if (cell === null) return "-";
      return (
        printCell(cell, {
          name: column.column.name,
          type: column.column.type ?? "text",
        }) ?? ""
      );
    }
    const result = other ? view.otherwise : view.rows[row]?.result;
    if (!result) return "";
    if (result.kind === "formula")
      return printFormula(result.output) ?? JSON.stringify(result.output);
    const cell = result.cells[column.index];
    const fixed = printResult(cell, column.column);
    return fixed ?? printFormula(cell) ?? JSON.stringify(cell);
  }
  /** A formula cell: shown with ƒ. */
  isFormula(row: number, col: number): boolean {
    const column = this.cols()[col];
    const view = this.view();
    if (!column || !view || column.kind === "name") return false;
    if (column.kind === "input") return this.merged(row);
    const result = this.isOtherwise(row)
      ? view.otherwise
      : view.rows[row]?.result;
    if (!result) return false;
    if (result.kind === "formula") return true;
    return printResult(result.cells[column.index], column.column) === null;
  }
  /** A cell that tests nothing: "-", and Otherwise's merged inputs. */
  isEmpty(row: number, col: number): boolean {
    if (this.isOtherwise(row) && this.cols()[col]?.kind === "input")
      return true;
    if (this.placeholder(row, col)) return true;
    return this.text(row, col) === "-";
  }
  /** The cell's place in the grid: a merged condition spans the inputs. */
  span(row: number, col: number): string | null {
    const column = this.cols()[col];
    const view = this.view();
    if (!column || !view) return null;
    // Otherwise tests nothing; a merged condition is one formula.
    if (column.kind === "input" && (this.isOtherwise(row) || this.merged(row)))
      return column.index === 0 ? `span ${view.inputs.length}` : null;
    if (column.kind === "result" && this.mergedResults(row))
      return column.index === 0 ? `span ${view.results.length}` : null;
    return "auto";
  }
  /** The cells a row draws: one per column, except the columns a merged cell covers. */
  rowCols(row: number): number[] {
    return this.cols()
      .map((_, index) => index)
      .filter((index) => this.span(row, index) !== null);
  }
  /** A cell's error, written or not (a duplicate rule name marks its name cell). */
  problemFor(row: number, col: number): string | null {
    return this.cellErrors().get(this.key(row, col)) ?? null;
  }
  private readonly cellErrors = computed(() => {
    const errors = new Map<string, string>();
    for (const problem of this.found())
      if (problem.severity === "error" && problem.at) {
        const key = this.key(problem.at.row, problem.at.col);
        if (!errors.has(key)) errors.set(key, problem.message);
      }
    return errors;
  });

  // ------------------------------------------------------------ publish
  readonly publishing = signal(false);
  /** Publish table: the grid's errors first, then the platform. */
  async publish() {
    // Off while the grid has errors: its description says why.
    if (this.publishing() || this.errorCount()) return;
    const tables = this.context().tables;
    if (!tables) return;
    this.publishing.set(true);
    try {
      await this.flush();
      const uses = this.shownFor || this.uses();
      const answer = await tables.publish(uses);
      if (this.lifetime.destroyed) return;
      if ("problem" in answer) this.notice.set(answer.problem);
      else if ("moved" in answer) {
        this.shownFor = answer.moved;
        this.notice.set(answer.message);
      } else this.notice.set(null);
    } finally {
      this.publishing.set(false);
    }
  }

  /** Why Publish table is off. */
  publishBlocked(): string {
    const count = this.errorCount();
    return count === 1
      ? "Fix the problem to publish"
      : `Fix the ${count} problems to publish`;
  }

  // ------------------------------------------------------------ problems
  problems(): (GridProblem & { at: GridCellAt | null })[] {
    const spec = this.spec();
    if (!spec) return [];
    const flagged = [...this.flagged()].map(([key, value]) => {
      const [row, col] = key.split(":").map(Number);
      return {
        severity: "error" as const,
        message: value.message,
        row,
        column: null,
        at: { row, col },
      };
    });
    const cols = this.cols();
    const rest = gridProblems(spec).map((problem) => {
      const row =
        problem.row ?? (problem.column !== null ? this.rowCount() - 1 : null);
      const col = problem.column
        ? cols.findIndex(
            (column) =>
              column.kind !== "name" && column.column.name === problem.column,
          )
        : 0;
      return {
        ...problem,
        at: row === null ? null : { row, col: Math.max(0, col) },
      };
    });
    return [...flagged, ...rest];
  }
  /** A message with its `quoted` parts shown as code. */
  words(message: string): { text: string; code: boolean }[] {
    return message
      .split("`")
      .map((text, index) => ({ text, code: index % 2 === 1 }))
      .filter((part) => part.text);
  }
  /** "2 problems", "1 problem, 1 warning", "1 warning": errors are what Publish table counts. */
  problemCount(): string {
    const errors = this.errorCount();
    const warnings = this.found().length - errors;
    const plural = (count: number, noun: string) =>
      `${count} ${noun}${count === 1 ? "" : "s"}`;
    return [
      errors ? plural(errors, "problem") : "",
      warnings ? plural(warnings, "warning") : "",
    ]
      .filter(Boolean)
      .join(", ");
  }
  goTo(at: GridCellAt) {
    this.focusCell(at.row, at.col);
  }

  // ---------------------------------------------------------------- focus
  isCursor(row: number, col: number): boolean {
    const cursor = this.cursor();
    return cursor.row === row && cursor.col === col;
  }
  /** Moves the cursor and focus at once, so keys typed right after land in the new cell. */
  focusCell(row: number, col: number) {
    this.cursor.set({ row, col });
    const find = () =>
      this.element.nativeElement.querySelector<HTMLElement>(
        `[data-cell="${row}:${col}"]`,
      );
    // The browser leaves a cell that is partly in view where it is, which can
    // be under the header or the columns that stay put: scroll it clear of
    // them (the scroll box's padding).
    const show = (cell: HTMLElement | null) => {
      cell?.focus({ preventScroll: true });
      cell?.scrollIntoView({ block: "nearest", inline: "nearest" });
    };
    const cell = find();
    if (cell) show(cell);
    else afterNextRender(() => show(find()), { injector: this.injector });
  }
  back() {
    void this.flush();
    this.context().closeParameterComponent?.();
  }
  focusBack() {
    this.element.nativeElement.querySelector<HTMLElement>(".dg-back")?.focus();
  }

  // -------------------------------------------------------------- editing
  editable(row: number, col: number): boolean {
    const column = this.cols()[col];
    if (!column || this.readOnly()) return false;
    if (this.isOtherwise(row))
      return column.kind === "result" && !this.mergedResults(row);
    if (column.kind === "name") return true;
    if (column.kind === "input")
      return this.merged(row) || column.column.type !== null;
    return !this.mergedResults(row);
  }
  startEdit(row: number, col: number, typed?: string) {
    if (!this.editable(row, col)) return;
    // A formula cell is edited in the formula editor, typed into or not.
    if (this.isFormula(row, col)) return this.openFormula(row, col);
    const shown = this.text(row, col);
    const original = shown === "-" ? "" : shown;
    this.cursor.set({ row, col });
    const cell = this.element.nativeElement.querySelector<HTMLElement>(
      `[data-cell="${row}:${col}"]`,
    );
    this.editing.set({
      row,
      col,
      text: typed ?? original,
      original,
      height: cell?.offsetHeight ?? null,
    });
    // Drawn now, so the keys typed next go into the field.
    this.changes.detectChanges();
    const field =
      this.element.nativeElement.querySelector<HTMLInputElement>(
        ".dg-cell-input",
      );
    field?.focus();
    if (typed === undefined) field?.select();
    else field?.setSelectionRange(typed.length, typed.length);
  }
  scrolled(event: Event) {
    const sideways = (event.target as HTMLElement).scrollLeft > 0;
    if (sideways !== this.scrolledSideways())
      this.scrolledSideways.set(sideways);
  }
  editInput(event: Event) {
    const editing = this.editing();
    if (editing)
      this.editing.set({
        ...editing,
        text: (event.target as HTMLInputElement).value,
      });
  }
  cancelEdit() {
    const editing = this.editing();
    if (!editing) return;
    this.editing.set(null);
    this.focusCell(editing.row, editing.col);
  }
  /** Writes the edited cell: a test that reads, a rule's new name; a cell that doesn't read stays flagged. */
  commitEdit(): boolean {
    const editing = this.editing();
    const spec = this.spec();
    if (!editing || !spec) return false;
    const { row, col, text } = editing;
    const column = this.cols()[col];
    this.editing.set(null);
    if (!column) return false;
    const key = this.key(row, col);
    const flagged = new Map(this.flagged());
    if (column.kind === "name") {
      flagged.delete(key);
      this.flagged.set(flagged);
      this.apply(renameRule(spec, row, text.trim()), key);
      return true;
    }
    if (column.kind === "input") {
      const reading = parseCell(text, {
        name: column.column.name,
        type: column.column.type ?? "text",
      });
      if (reading.kind === "formula") {
        const read = tableFormula(reading.text, spec.inputSchema);
        if (!read.ok) {
          flagged.set(key, { text, message: read.message });
          this.flagged.set(flagged);
          return false;
        }
        flagged.delete(key);
        this.flagged.set(flagged);
        // The other cells and the formula together: the rule becomes one formula.
        const cells =
          decomposeWhen(spec.rules[row].when, this.testable()) ?? [];
        cells[column.index] = null;
        const rest = composeWhen(cells);
        const parts =
          isRecord(rest) && "literal" in rest
            ? []
            : isRecord(rest) &&
                isRecord(rest["op"]) &&
                rest["op"]["name"] === "and"
              ? (rest["op"]["args"] as Expression[])
              : [rest];
        const when: Expression = parts.length
          ? { op: { name: "and", args: [...parts, read.expression] } }
          : read.expression;
        this.apply(setRuleFormula(spec, row, when), key);
        return true;
      }
      if (reading.kind === "error") {
        flagged.set(key, { text, message: reading.message });
        this.flagged.set(flagged);
        return false;
      }
      let next: TableSpec;
      try {
        next = setCondition(
          spec,
          row,
          column.index,
          reading.kind === "empty" ? null : reading.expression,
        );
      } catch (error) {
        flagged.set(key, {
          text,
          message: error instanceof Error ? error.message : String(error),
        });
        this.flagged.set(flagged);
        return false;
      }
      flagged.delete(key);
      this.flagged.set(flagged);
      this.apply(next, key);
      return true;
    }
    const reading = parseResult(text, column.column);
    let value: Expression | undefined;
    if (reading.kind === "formula") {
      const read = tableFormula(reading.text, spec.inputSchema);
      if (!read.ok) {
        flagged.set(key, { text, message: read.message });
        this.flagged.set(flagged);
        return false;
      }
      value = read.expression;
    } else if (reading.kind === "error") {
      flagged.set(key, { text, message: reading.message });
      this.flagged.set(flagged);
      return false;
    } else value = reading.kind === "empty" ? undefined : reading.expression;
    flagged.delete(key);
    this.flagged.set(flagged);
    this.apply(
      setResult(
        spec,
        this.isOtherwise(row) ? "otherwise" : row,
        column.index,
        value,
      ),
      key,
    );
    return true;
  }
  /** The input columns as the cell language reads them. */
  private testable() {
    return (this.view()?.inputs ?? []).map((column) => ({
      name: column.name,
      type: column.type ?? ("text" as const),
    }));
  }

  // ------------------------------------------------------------- formulas
  openFormula(row: number, col: number) {
    const column = this.cols()[col];
    const view = this.view();
    if (!column || !view || column.kind === "name") return;
    const condition = column.kind === "input";
    const result = this.isOtherwise(row)
      ? view.otherwise
      : view.rows[row]?.result;
    const rule = view.rows[row]?.condition;
    const expression: Expression | null | undefined = condition
      ? rule?.kind === "formula"
        ? rule.when
        : null
      : result?.kind === "cells"
        ? result.cells[column.index]
        : null;
    this.formula.set({
      row,
      col,
      heading: condition
        ? `Edit formula: ${this.rowLabel(row)} condition`
        : `Edit formula: ${this.rowLabel(row)} · ${column.column.name}`,
      text: expression ? (printFormula(expression) ?? "") : "",
    });
  }
  formulaApplied(expression: Expression) {
    const open = this.formula();
    const spec = this.spec();
    const column = open ? this.cols()[open.col] : null;
    this.formula.set(null);
    if (!open || !spec || !column || column.kind === "name") return;
    this.apply(
      column.kind === "input"
        ? setRuleFormula(spec, open.row, expression)
        : setResult(
            spec,
            this.isOtherwise(open.row) ? "otherwise" : open.row,
            column.index,
            expression,
          ),
      this.key(open.row, open.col),
    );
    this.focusCell(open.row, open.col);
  }
  formulaClosed() {
    const open = this.formula();
    this.formula.set(null);
    if (open) this.focusCell(open.row, open.col);
  }
  /** A merged condition shown as cells again, when each part tests one column. */
  split(row: number) {
    const spec = this.spec();
    const rule = spec?.rules[row];
    if (!spec || !rule) return;
    const when = splitIntoCells(rule.when, this.testable());
    if (when) this.apply(setRuleFormula(spec, row, when));
  }
  /** Escape on a flagged cell gives back the last cell that was written. */
  restore(row: number, col: number) {
    const key = this.key(row, col);
    if (!this.flagged().has(key)) return false;
    const flagged = new Map(this.flagged());
    flagged.delete(key);
    this.flagged.set(flagged);
    return true;
  }

  // -------------------------------------------------------------- writing
  /** Applies an edit: in the draft, or by starting the draft of the next version first. */
  apply(next: TableSpec, key = "command", record = true) {
    const before = this.spec();
    if (!before || next === before || this.readOnly()) return;
    // The account the edit was made in: its save goes there or nowhere.
    this.editedIn ??= this.context().tables?.account() ?? null;
    if (record) this.history.record(before, key, Date.now());
    this.spec.set(next);
    this.revision.update((revision) => revision + 1);
    const where = this.where();
    if (where === "draft" || this.starting) return this.scheduleSave();
    const tables = this.context().tables;
    if (!tables) return;
    const uses = this.uses();
    const started = tables.startDraft(uses).then((answer) => {
      if ("problem" in answer) {
        if (this.starting === started) {
          this.starting = null;
          this.spec.set(before);
          this.revision.update((revision) => revision + 1);
          this.notice.set(answer.problem);
        }
        return null;
      }
      if (this.starting === started) {
        this.starting = null;
        this.shownFor = answer.uses;
        this.announcement.set(
          `Started ${nameOf(answer.uses)} ${versionOf(answer.uses)} on this computer.`,
        );
        this.scheduleSave();
      }
      return answer.uses;
    });
    this.starting = started;
  }
  private scheduleSave() {
    if (this.saveTimer) clearTimeout(this.saveTimer);
    this.saveTimer = setTimeout(() => void this.flush(), SAVE_DELAY);
  }
  /** Keeps the table in the draft now (on Back, on close, before publishing). */
  async flush(): Promise<void> {
    if (this.saveTimer) clearTimeout(this.saveTimer);
    this.saveTimer = null;
    if (this.starting) await this.starting;
    const spec = this.spec();
    const uses = this.shownFor;
    const tables = this.context().tables;
    if (!spec || !tables || tables.where(uses) !== "draft") return;
    const account = this.editedIn ?? tables.account();
    this.editedIn = null;
    const problem = await tables.save(uses, spec as unknown as Json, account);
    if (problem && !this.lifetime.destroyed && this.shownFor === uses)
      this.notice.set(problem);
  }

  // ------------------------------------------------------------- commands
  setPolicy(value: string) {
    const spec = this.spec();
    if (!spec) return;
    const { spec: next, removedOtherwise } = setHitPolicy(
      spec,
      value as HitPolicy,
    );
    this.apply(next);
    if (removedOtherwise)
      this.context().notify?.("Removed the Otherwise result.", {
        label: "Undo",
        run: () => {
          if (this.spec() === next) this.apply(spec);
        },
      });
  }
  addRuleAfter(row: number) {
    const spec = this.spec();
    if (!spec) return;
    const at = Math.min(row, spec.rules.length - 1);
    this.apply(addRule(spec, at));
    this.focusCell(at + 1, 0);
  }
  removeRuleAt(row: number) {
    const spec = this.spec();
    if (!spec) return;
    try {
      this.apply(removeRule(spec, row));
    } catch (error) {
      this.notice.set(error instanceof Error ? error.message : String(error));
    }
  }
  openColumnDialog(request: ColumnRequest) {
    this.dialog.set(request);
  }
  columnDone(answer: ColumnAnswer) {
    const request = this.dialog();
    const spec = this.spec();
    if (!request || !spec) return;
    try {
      let next: TableSpec;
      if (request.mode === "add")
        next =
          request.side === "input"
            ? addInputColumn(spec, answer.name, answer.type)
            : addResultColumn(spec, answer.name, answer.type);
      else if (request.mode === "rename")
        next =
          request.side === "input"
            ? renameInputColumn(spec, request.name!, answer.name)
            : renameResultColumn(spec, request.name!, answer.name);
      else next = setInputColumnType(spec, request.name!, answer.type);
      this.dialog.set(null);
      this.apply(next);
    } catch (error) {
      this.notice.set(error instanceof Error ? error.message : String(error));
      this.dialog.set(null);
    }
  }
  /** Types a column's cells all still read as. */
  private unavailableTypes(name: string): Partial<Record<ColumnType, string>> {
    const view = this.view();
    const index =
      view?.inputs.findIndex((column) => column.name === name) ?? -1;
    if (!view || index < 0) return {};
    const result: Partial<Record<ColumnType, string>> = {};
    for (const type of Object.keys(COLUMN_TYPE_LABELS) as ColumnType[]) {
      const column = { name, type };
      const fails = view.rows.some((row) => {
        if (row.condition.kind !== "cells") return false;
        const cell = row.condition.cells[index];
        if (cell === null) return false;
        const text = printCell(
          cell,
          view.inputs[index].type
            ? { name, type: view.inputs[index].type! }
            : column,
        );
        return text === null || parseCell(text, column).kind !== "test";
      });
      if (fails) result[type] = "Some cells don't read as this type.";
    }
    return result;
  }
  columnMenu(col: GridCol): RowMenuItem[] {
    if (col.kind === "name") return [];
    const side = col.kind;
    const name = col.column.name;
    const items: RowMenuItem[] = [
      {
        label: "Rename",
        run: () => this.openColumnDialog({ mode: "rename", side, name }),
      },
    ];
    if (side === "input")
      items.push({
        label: "Change type",
        disabled: col.column.type === null,
        detail:
          col.column.type === null
            ? "Studio can't change this field's type."
            : undefined,
        run: () =>
          this.openColumnDialog({
            mode: "type",
            side,
            name,
            type: col.column.type ?? "text",
            unavailable: this.unavailableTypes(name),
          }),
      });
    items.push({
      label: "Remove",
      danger: true,
      run: () => {
        const spec = this.spec();
        if (!spec) return;
        this.apply(
          side === "input"
            ? removeInputColumn(spec, name)
            : removeResultColumn(spec, name),
        );
      },
    });
    return items;
  }
  ruleMenu(row: number): RowMenuItem[] {
    const spec = this.spec();
    const merged = this.merged(row);
    const fits =
      merged && spec?.rules[row]
        ? splitIntoCells(spec.rules[row].when, this.testable()) !== null
        : false;
    return [
      ...(merged
        ? [
            { label: "Edit formula", run: () => this.openFormula(row, 1) },
            {
              label: "Split into cells",
              disabled: !fits,
              detail: fits
                ? undefined
                : "This condition doesn't fit the cells; keep it as a formula.",
              run: () => this.split(row),
            },
          ]
        : []),
      { label: "Add rule below", run: () => this.addRuleAfter(row) },
      {
        label: "Remove rule",
        danger: true,
        disabled: (this.spec()?.rules.length ?? 0) <= 1,
        detail:
          (this.spec()?.rules.length ?? 0) <= 1
            ? "A table keeps at least one rule."
            : undefined,
        run: () => this.removeRuleAt(row),
      },
    ];
  }

  // ----------------------------------------------------------------- keys
  /** The grid's keys, from the keymap's decision grid rows; Ctrl/Cmd+Enter does nothing here. */
  cellKey(event: KeyboardEvent, row: number, col: number) {
    const editing = this.editing();
    if (editing) {
      if (event.key === "Enter" && !event.altKey) {
        this.handled(event);
        this.commitEdit();
        this.focusCell(row, col);
      } else if (event.key === "Escape") {
        this.handled(event);
        this.cancelEdit();
      } else if (event.key === "Tab") {
        this.handled(event);
        this.commitEdit();
        this.move(event.shiftKey ? "previousCell" : "nextCell", row, col);
      }
      return;
    }
    if (event.key === "Escape") {
      this.handled(event);
      if (!this.restore(row, col)) this.focusBack();
      return;
    }
    const command = commandFor(
      eventCombo(event, KEY_PLATFORM),
      "decisionGrid",
      { platform: KEY_PLATFORM },
    );
    switch (command) {
      case "cellUp":
      case "cellDown":
      case "cellLeft":
      case "cellRight":
      case "nextCell":
      case "previousCell":
        this.handled(event);
        return this.move(command, row, col);
      case "editCell":
        this.handled(event);
        return this.startEdit(row, col);
      case "clearCell":
        this.handled(event);
        return this.clearCell(row, col);
      case "addRule":
        this.handled(event);
        return this.addRuleAfter(row);
      case "moveRuleUp":
      case "moveRuleDown":
        this.handled(event);
        return this.moveRuleBy(row, col, command === "moveRuleUp" ? -1 : 1);
      case "duplicateRule":
        this.handled(event);
        return this.duplicateAt(row, col);
      case "undo":
      case "redo":
        this.handled(event);
        return this.step(command);
    }
    // Typing a character starts editing with it, as a spreadsheet does.
    if (
      event.key.length === 1 &&
      !event.ctrlKey &&
      !event.metaKey &&
      !event.altKey &&
      this.editable(row, col)
    ) {
      this.handled(event);
      this.startEdit(row, col, event.key);
    }
  }
  private handled(event: KeyboardEvent) {
    event.preventDefault();
    event.stopPropagation();
  }
  private move(command: CursorCommand, row: number, col: number) {
    const view = this.view();
    if (!view) return;
    const rows = view.rows.length;
    const otherwise = !!view.otherwise;
    const drawn = this.rowCols(row);
    // The cell that covers a column in a row (a merged cell covers several).
    const covering = (r: number, c: number) =>
      this.rowCols(r)
        .filter((index) => index <= c)
        .at(-1) ?? 0;
    const horizontal = command !== "cellUp" && command !== "cellDown";
    const next = moveCursor(
      horizontal
        ? { row, column: Math.max(0, drawn.indexOf(col)) }
        : { row, column: col },
      command,
      {
        rows,
        columns: horizontal ? drawn.length : this.cols().length,
        otherwise,
      },
    );
    if (next === "leave-forward")
      return this.element.nativeElement
        .querySelector<HTMLElement>(".dg-add-rule")
        ?.focus();
    if (next === "leave-backward") {
      // Back to the control before the grid: the policy, or Back to the step.
      const root = this.element.nativeElement;
      return (
        root.querySelector<HTMLElement>("#dg-policy") ??
        root.querySelector<HTMLElement>(".dg-back")
      )?.focus();
    }
    if (!horizontal) return this.focusCell(next.row, covering(next.row, col));
    if (next.row === row) return this.focusCell(row, drawn[next.column]);
    // Wrapped to the next or previous row.
    const there = this.rowCols(next.row);
    this.focusCell(
      next.row,
      command === "previousCell" ? (there.at(-1) ?? 0) : (there[0] ?? 0),
    );
  }
  clearCell(row: number, col: number) {
    if (!this.editable(row, col)) return;
    const column = this.cols()[col];
    const spec = this.spec();
    if (!column || !spec || column.kind === "name") return;
    this.restore(row, col);
    this.apply(
      column.kind === "input"
        ? setCondition(spec, row, column.index, null)
        : setResult(
            spec,
            this.isOtherwise(row) ? "otherwise" : row,
            column.index,
            undefined,
          ),
      this.key(row, col),
    );
  }
  private moveRuleBy(row: number, col: number, delta: -1 | 1) {
    const spec = this.spec();
    if (!spec || this.isOtherwise(row)) return;
    const next = moveRule(spec, row, delta);
    if (next === spec) return;
    this.apply(next);
    this.say(`Moved rule ${row + 1} to position ${row + delta + 1}.`);
    this.focusCell(row + delta, col);
  }
  private duplicateAt(row: number, col: number) {
    const spec = this.spec();
    if (!spec || this.isOtherwise(row)) return;
    this.apply(duplicateRule(spec, row));
    this.focusCell(row + 1, col);
  }
  private step(command: "undo" | "redo") {
    const current = this.spec();
    if (!current) return;
    const other =
      command === "undo"
        ? this.history.undo(current)
        : this.history.redo(current);
    if (!other)
      return this.say(
        command === "undo"
          ? "Nothing to undo in the table."
          : "Nothing to redo in the table.",
      );
    this.flagged.set(new Map());
    this.apply(other, command, false);
  }
  private say(text: string) {
    this.announcement.set("");
    setTimeout(() => {
      if (!this.lifetime.destroyed) this.announcement.set(text);
    });
  }
  private pastes = 0;
  /** Pasting tab-separated cells fills a block from the focused cell, as one undo step. */
  paste(event: ClipboardEvent) {
    if (this.editing() || this.readOnly()) return;
    const text = event.clipboardData?.getData("text/plain") ?? "";
    const spec = this.spec();
    const view = this.view();
    if (!text || !spec || !view) return;
    event.preventDefault();
    const cursor = this.cursor();
    const {
      spec: next,
      flagged,
      notes,
    } = pasteInto(
      spec,
      { row: cursor.row, column: cursor.col },
      parsePaste(text),
      view,
    );
    const marks = new Map(this.flagged());
    for (const problem of flagged)
      marks.set(this.key(problem.row, problem.column), {
        text: problem.text,
        message: problem.message,
      });
    this.flagged.set(marks);
    // Every paste is its own undo step.
    this.apply(next, `paste:${++this.pastes}`);
    this.notice.set(notes.length ? notes.join(" ") : null);
  }
}
