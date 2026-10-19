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
// What autocomplete offers at the caret, from the field's scope: fields with
// their type after `input.` or `.output.`, visible steps nearest first after
// `steps.`, the loop roots inside a loop body, functions and keywords where a
// value starts, `and`/`or` after one, and an enum field's values after `==`.
// Inside a template, only between `{{` and `}}`.
import type { Schema, ScopeEntry, VisibleStep } from "../../forms/core/scope";
import { schemaChild } from "../../forms/core/scope";
import { functionList } from "./grammar";
import { referenceSpans } from "./spans";
import { tokenize, type Token, type TokenMode } from "./tokens";

export interface CompletionOption {
  label: string;
  detail?: string;
  kind: "field" | "step" | "root" | "function" | "keyword" | "value";
  /** The text that replaces the list's range. */
  apply: string;
  /** Where the caret goes inside `apply`; its end when absent. */
  caret?: number;
}
export interface CompletionList {
  from: number;
  to: number;
  options: CompletionOption[];
}
/** A scope entry from the Studio host (loop bodies). */
export interface HostEntry {
  ref: string;
  typeLabel: string;
  schema: Schema;
}
export interface CompletionScope {
  entries: readonly ScopeEntry[];
  /** Visible steps in the order they run. */
  steps: readonly VisibleStep[];
  /** The loops around the field, innermost first. */
  loops: readonly string[];
  loopEntries: readonly HostEntry[];
  features: readonly string[];
  inputSchema: Schema;
}

const SEGMENT = /^[A-Za-z_][A-Za-z0-9_-]*$/;
const INDEX = /^(?:0|[1-9][0-9]*)$/;
const OPERAND_ENDS = new Set(["name", "segment", "number", "string"]);
const isPunct = (token: Token | undefined, text: string) =>
  token?.type === "punct" && token.text === text;
const accessor = (key: string) =>
  SEGMENT.test(key)
    ? `.${key}`
    : INDEX.test(key)
      ? `[${key}]`
      : `[${JSON.stringify(key)}]`;
const parentOf = (pointer: string) =>
  pointer.slice(0, pointer.lastIndexOf("/"));
const lastKey = (pointer: string) =>
  pointer
    .slice(pointer.lastIndexOf("/") + 1)
    .replace(/~1/g, "/")
    .replace(/~0/g, "~");

function filter(
  options: CompletionOption[],
  partial: string,
): CompletionOption[] {
  if (!partial) return options.slice(0, 50);
  const lower = partial.toLowerCase();
  const starts = options.filter((option) =>
    option.label.toLowerCase().startsWith(lower),
  );
  const contains = options.filter(
    (option) =>
      !option.label.toLowerCase().startsWith(lower) &&
      option.label.toLowerCase().includes(lower),
  );
  return [...starts, ...contains].slice(0, 50);
}

function children(pointer: string, scope: CompletionScope): CompletionOption[] {
  const entries: { ref: string; typeLabel: string }[] = [
    ...scope.entries,
    ...scope.loopEntries,
  ];
  return entries
    .filter((entry) => parentOf(entry.ref) === pointer && entry.ref !== pointer)
    .map((entry) => {
      const key = lastKey(entry.ref);
      return {
        label: key,
        detail: entry.typeLabel,
        kind: "field" as const,
        apply: accessor(key),
      };
    });
}

function accessorOptions(
  pointer: string,
  scope: CompletionScope,
): CompletionOption[] {
  const segments = pointer
    .slice(1)
    .split("/")
    .map((segment) => segment.replace(/~1/g, "/").replace(/~0/g, "~"));
  if (pointer === "/steps")
    return [...scope.steps].reverse().map((step) => ({
      label: step.id,
      detail: step.kind,
      kind: "step" as const,
      apply: accessor(step.id),
    }));
  if (segments[0] === "steps" && segments.length === 2)
    return scope.steps.some((step) => step.id === segments[1])
      ? [
          {
            label: "output",
            detail: "The step's result",
            kind: "field",
            apply: ".output",
          },
        ]
      : [];
  if (pointer === "/loops")
    return scope.loops.map((id) => ({
      label: id,
      detail: "Loop",
      kind: "step" as const,
      apply: accessor(id),
    }));
  if (segments[0] === "loops" && segments.length === 2)
    return scope.loops.includes(segments[1])
      ? [
          {
            label: "item",
            detail: "Current item",
            kind: "field",
            apply: ".item",
          },
          {
            label: "index",
            detail: "Item position",
            kind: "field",
            apply: ".index",
          },
        ]
      : [];
  return children(pointer, scope);
}

/** The values of an enum field compared with `==` or `!=`; `index` is the comparison's token. */
function enumValues(
  tokens: Token[],
  index: number,
  scope: CompletionScope,
): CompletionOption[] {
  const comparison = tokens[index];
  if (
    comparison?.type !== "op" ||
    (comparison.text !== "==" && comparison.text !== "!=")
  )
    return [];
  const before = tokens[index - 1];
  const span = referenceSpans(tokens.slice(0, index)).find(
    (item) => item.to === before?.to,
  );
  if (!span) return [];
  const segments = span.pointer
    .slice(1)
    .split("/")
    .map((s) => s.replace(/~1/g, "/").replace(/~0/g, "~"));
  let schema: Schema | null = null;
  if (segments[0] === "input") {
    schema = scope.inputSchema;
    for (const segment of segments.slice(1)) {
      const child = schemaChild(schema, scope.inputSchema, segment);
      schema = child.kind === "found" ? child.schema : null;
      if (!schema) break;
    }
  } else {
    const entry = [...scope.entries, ...scope.loopEntries].find(
      (item) => item.ref === span.pointer,
    );
    schema = entry?.schema ?? null;
  }
  const values = Array.isArray(schema?.["enum"])
    ? (schema!["enum"] as unknown[])
    : [];
  return values.map((value) => ({
    label: JSON.stringify(value),
    detail: "Choice",
    kind: "value" as const,
    apply: JSON.stringify(value),
  }));
}

export function completeAt(
  text: string,
  caret: number,
  mode: TokenMode,
  scope: CompletionScope,
): CompletionList | null {
  const tokens = tokenize(text, mode).filter((token) => token.from < caret);
  if (mode === "template") {
    let inside = false;
    for (const token of tokens) {
      if (token.type === "open") inside = true;
      if (token.type === "close" && token.to <= caret) inside = false;
    }
    if (!inside) return null;
  }
  const last = tokens[tokens.length - 1];
  if (
    last &&
    last.to >= caret &&
    ["string", "text", "error"].includes(last.type)
  )
    return null;
  const partial =
    last &&
    last.to === caret &&
    (last.type === "name" || last.type === "segment")
      ? last
      : null;
  const end = partial ? tokens.length - 1 : tokens.length;
  const previous = tokens[end - 1];
  const from = partial ? partial.from : caret;
  const typed = partial ? text.slice(partial.from, caret) : "";
  if (isPunct(previous, ".")) {
    const span = referenceSpans(tokens.slice(0, end - 1)).find(
      (item) => item.to === previous!.from,
    );
    if (!span) return null;
    const options = filter(accessorOptions(span.pointer, scope), typed);
    return options.length ? { from: previous!.from, to: caret, options } : null;
  }
  const afterValue =
    !!previous &&
    ((OPERAND_ENDS.has(previous.type) &&
      !["and", "or", "not"].includes(previous.text)) ||
      isPunct(previous, ")") ||
      isPunct(previous, "]") ||
      isPunct(previous, "}") ||
      previous.type === "tick");
  if (afterValue) {
    const options = filter(
      ["and", "or"].map((word) => ({
        label: word,
        kind: "keyword" as const,
        apply: word,
      })),
      typed,
    );
    return options.length && (typed || text[caret - 1] === " ")
      ? { from, to: caret, options }
      : null;
  }
  const roots: CompletionOption[] = [
    { label: "input", detail: "Workflow input", kind: "root", apply: "input" },
    ...(scope.steps.length
      ? [
          {
            label: "steps",
            detail: "Earlier steps",
            kind: "root" as const,
            apply: "steps",
          },
        ]
      : []),
    ...(scope.loops.length
      ? [
          {
            label: "item",
            detail: "Current item",
            kind: "root" as const,
            apply: "item",
          },
          {
            label: "index",
            detail: "Item position",
            kind: "root" as const,
            apply: "index",
          },
          {
            label: "loops",
            detail: "Loops around this step",
            kind: "root" as const,
            apply: "loops",
          },
        ]
      : []),
  ];
  const functions = functionList(scope.features).map((name) => ({
    label: name,
    detail: "Function",
    kind: "function" as const,
    apply: `${name}()`,
    caret: name.length + 1,
  }));
  const keywords = ["not", "true", "false", "null"].map((word) => ({
    label: word,
    kind: "keyword" as const,
    apply: word,
  }));
  const options = filter(
    [
      ...enumValues(tokens, end - 1, scope),
      ...roots,
      ...functions,
      ...keywords,
    ],
    typed,
  );
  return options.length ? { from, to: caret, options } : null;
}
