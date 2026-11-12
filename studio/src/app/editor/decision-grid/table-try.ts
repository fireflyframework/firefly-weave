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
// Trying the table, shared by the grid and the Input and Output panes of
// table view: the test values Input holds, where they came from, and how
// Output shows the result. What is tried lives in memory only.
import { signal, type Signal } from "@angular/core";
import type { Schema } from "../../forms/core/scope";
import { uiKey, type BrowserStorage } from "../state/browser-store";
import { studioStorage } from "../state/studio-storage";
import type { GridView, TableSpec } from "./rules-model";
import type { TryView } from "./try-view";

/** How Output shows the result: a table of fields, or JSON. */
export type ResultView = "table" | "json";
export const RESULT_VIEW_KEY = uiKey("weave.decisionTry.view.v1");

const defaultStorage = (): BrowserStorage | null => {
  try {
    return studioStorage();
  } catch {
    return null;
  }
};
export function readResultView(
  storage: Pick<Storage, "getItem"> | null = defaultStorage(),
): ResultView {
  try {
    return storage?.getItem(RESULT_VIEW_KEY) === "json" ? "json" : "table";
  } catch {
    return "table";
  }
}
function writeResultView(
  view: ResultView,
  storage: Pick<Storage, "setItem"> | null = defaultStorage(),
) {
  try {
    storage?.setItem(RESULT_VIEW_KEY, view);
  } catch {
    // The choice holds for this session only.
  }
}

/** What the grid gives the panes. */
export interface TryGrid {
  readonly spec: Signal<TableSpec | null>;
  readonly view: Signal<GridView | null>;
  /** The try as Output shows it, and each rule's mark. */
  readonly shown: Signal<TryView | null>;
  readonly readOnly: Signal<boolean>;
  /** Moves the grid's cursor to a rule's first cell (a rule card). */
  goToRule(row: number): void;
}

const blank = (value: unknown) =>
  value === undefined || value === null || value === "";
const same = (a: Record<string, unknown>, b: Record<string, unknown>) => {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  return [...keys].every(
    (key) =>
      (blank(a[key]) && blank(b[key])) ||
      JSON.stringify(a[key]) === JSON.stringify(b[key]),
  );
};

export class TableTry {
  /** What Input's fields start from; a new object starts them again. */
  readonly initial = signal<Record<string, unknown>>({});
  /** The step's test data, when it has some. */
  readonly testData = signal<Record<string, unknown> | null>(null);
  /** The values tried now. */
  readonly value = signal<Record<string, unknown>>({});
  /** True once a value differs from the step's test data. */
  readonly edited = signal(false);
  readonly resultView = signal<ResultView>(readResultView());

  constructor(readonly grid: TryGrid) {}

  /** Starts from the step's test data, or from empty fields. */
  start(testData: Record<string, unknown> | null) {
    const usable = testData && Object.keys(testData).length ? testData : null;
    this.testData.set(usable);
    this.initial.set({ ...(usable ?? {}) });
    this.value.set({ ...(usable ?? {}) });
    this.edited.set(false);
  }
  change(value: Record<string, unknown>) {
    this.value.set(value);
    const testData = this.testData();
    this.edited.set(!!testData && !same(value, testData));
  }
  /**
   * The table's input fields changed from `before` to `after`: the values
   * keep only fields the table still has, a renamed field's value moves to
   * its new name, and Input's fields start again from them.
   */
  reshape(before: readonly string[], after: readonly string[]) {
    const removed = before.filter((name) => !after.includes(name));
    const added = after.filter((name) => !before.includes(name));
    const renamed =
      removed.length === 1 && added.length === 1
        ? { from: removed[0], to: added[0] }
        : null;
    const fit = (values: Record<string, unknown>) =>
      Object.fromEntries(
        Object.entries(values).flatMap(([name, value]): [string, unknown][] =>
          renamed && name === renamed.from
            ? [[renamed.to, value]]
            : after.includes(name)
              ? [[name, value]]
              : [],
        ),
      );
    const testData = this.testData();
    if (testData) this.testData.set(fit(testData));
    const value = fit(this.value());
    this.initial.set({ ...value });
    this.change(value);
  }
  /** Use the step's test data: the fields start again from it. */
  useTestData() {
    this.start(this.testData());
  }
  /** True while no field holds a value. */
  empty(): boolean {
    return Object.values(this.value()).every(blank);
  }
  setResultView(view: ResultView) {
    this.resultView.set(view);
    writeResultView(view);
  }
  schema(): Schema {
    return (this.grid.spec()?.inputSchema ?? {}) as Schema;
  }
  outputSchema(): Schema {
    return (this.grid.spec()?.outputSchema ?? {}) as Schema;
  }
}
