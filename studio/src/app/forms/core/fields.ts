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
import { decode, encode, type Bound } from "./binding";

export interface FieldRow {
  id: number;
  name: string;
  bound: Bound;
  expression: unknown;
  originalName: string;
}
let sequence = 0;

/** Local unnamed rows never change the expression or block another edit. */
export class FieldsDraft {
  rows: FieldRow[];
  private edited = false;
  constructor(private readonly source: unknown) {
    const bound = decode(source);
    this.rows =
      bound.kind === "object"
        ? Object.entries(bound.entries).map(([name, child]) =>
            this.row(name, child),
          )
        : [];
  }
  private row(name: string, bound: Bound): FieldRow {
    return {
      id: ++sequence,
      name,
      originalName: name,
      bound,
      expression: encode(bound),
    };
  }
  add(): FieldRow {
    const row = this.row("", { kind: "value", value: "" });
    this.rows.push(row);
    return row;
  }
  rename(row: FieldRow, name: string) {
    if (row.name === name) return;
    row.name = name;
    this.edited = true;
  }
  update(row: FieldRow, expression: unknown) {
    row.bound = decode(expression);
    row.expression = expression;
    if (row.name || row.originalName) this.edited = true;
  }
  move(row: FieldRow, direction: number) {
    const index = this.rows.indexOf(row);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= this.rows.length) return;
    this.rows.splice(index, 1);
    this.rows.splice(target, 0, row);
    if (row.name) this.edited = true;
  }
  remove(row: FieldRow) {
    this.rows = this.rows.filter((item) => item !== row);
    if (row.name || row.originalName) this.edited = true;
  }
  error(row: FieldRow): string {
    if (!row.name && row.originalName) return "Enter a field name.";
    return row.name &&
      this.rows.some((other) => other !== row && other.name === row.name)
      ? "Use a unique field name."
      : "";
  }
  get valid(): boolean {
    return this.rows.every((row) => !this.error(row));
  }
  expression(): unknown {
    if (!this.edited) return this.source;
    return encode({
      kind: "object",
      entries: Object.fromEntries(
        this.rows.filter((row) => row.name).map((row) => [row.name, row.bound]),
      ),
    });
  }
}
