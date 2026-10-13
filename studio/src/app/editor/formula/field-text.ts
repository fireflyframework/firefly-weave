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
// What a field's text means, and the text a stored value shows as. A
// formula that is only a literal ("Ready", 25) is a Fixed value, except in
// fields that take formulas only (Loop items), where it stays a formula.
// Template text is plain text with {{ formula }} placeholders.
import type { Json } from "../../forms/core/json";
import type { ParamValue } from "../ndv/params/value-io";
import {
  parseFormula,
  parseTemplate,
  type FormulaError,
  type ParseOptions,
} from "./grammar";
import { printFormula, printJson } from "./printer";
import { escapeTemplateText, printTemplate } from "./template";

export interface FieldText {
  mode: "formula" | "template";
  options: ParseOptions;
  /** The field holds formulas only (Loop items, file references). */
  formulaOnly: boolean;
}
export type ReadText =
  | { kind: "empty" }
  | { kind: "value"; value: ParamValue; literal: boolean }
  | { kind: "error"; error: FormulaError };

export function readFieldText(text: string, field: FieldText): ReadText {
  const result =
    field.mode === "template"
      ? parseTemplate(text, field.options)
      : parseFormula(text, field.options);
  if (result.kind === "error") return { kind: "error", error: result.error };
  if (result.kind === "empty") return { kind: "empty" };
  const expression = result.expression;
  const literal =
    Object.keys(expression).length === 1 && "literal" in expression;
  if (literal && !field.formulaOnly)
    return {
      kind: "value",
      value: {
        mode: "fixed",
        value: (expression as { literal: Json }).literal,
      },
      literal,
    };
  return { kind: "value", value: { mode: "mapped", expression }, literal };
}

/** The text a stored value shows as in the editor; null when the editor can't show it. */
export function fieldText(
  value: ParamValue,
  mode: "formula" | "template",
): string | null {
  if (value.mode === "absent") return "";
  if (value.mode === "fixed") {
    if (mode === "formula") return printJson(value.value);
    if (typeof value.value === "string")
      return escapeTemplateText(value.value, "end");
    const json = printJson(value.value);
    return json === null ? null : `{{ ${json} }}`;
  }
  return mode === "template"
    ? printTemplate(value.expression)
    : printFormula(value.expression);
}
