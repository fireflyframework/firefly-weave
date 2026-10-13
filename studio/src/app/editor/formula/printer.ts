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
// The canonical text of a Weave expression tree. Parsing what this prints
// gives back the same tree, for every tree the language accepts. Trees the
// language doesn't know (extra keys, unknown operators, values that aren't
// JSON) print as null: Studio shows them read-only and never rewrites them.
import { ARITY, ROOTS, SYMBOLS } from "./grammar";
import { IDENTIFIER, MAX_SAFE_INTEGER } from "./tokens";

const SEGMENT = /^[A-Za-z_][A-Za-z0-9_-]*$/;
const INDEX = /^(?:0|[1-9][0-9]*)$/;
const POINTER = /^(?:\/(?:[^~/]|~[01])*)*$/;
const LOGICAL = new Set(["and", "or", "not"]);
const UNPRINTABLE = Symbol("unprintable");

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const isLiteral = (node: unknown): boolean =>
  isRecord(node) && Object.keys(node).length === 1 && "literal" in node;

/** The operator a node applies, or null when it isn't an operation. */
export function opName(node: unknown): string | null {
  if (!isRecord(node) || Object.keys(node).length !== 1) return null;
  const op = node["op"];
  return isRecord(op) && typeof op["name"] === "string" ? op["name"] : null;
}

export const printKey = (key: string): string =>
  IDENTIFIER.test(key) ? key : JSON.stringify(key);

function number(value: number): string {
  if (!Number.isFinite(value)) throw UNPRINTABLE;
  return Number.isInteger(value) && Math.abs(value) > MAX_SAFE_INTEGER
    ? value.toExponential()
    : JSON.stringify(value);
}

function json(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "boolean") return String(value);
  if (typeof value === "number") return number(value);
  if (typeof value === "string") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(json).join(", ")}]`;
  if (isRecord(value))
    return `{${Object.keys(value)
      .map((key) => `${printKey(key)}: ${json(value[key])}`)
      .join(", ")}}`;
  throw UNPRINTABLE;
}

/** A JSON value as formula text, or null when it isn't JSON. */
export function printJson(value: unknown): string | null {
  try {
    return json(value);
  } catch (error) {
    if (error === UNPRINTABLE) return null;
    throw error;
  }
}

/** A reference as a path (input.a, steps["x.y"].output) or ref("…"). */
export function printRef(pointer: string): string {
  const call = `ref(${JSON.stringify(pointer)})`;
  if (!POINTER.test(pointer) || pointer === "") return call;
  const segments = pointer
    .slice(1)
    .split("/")
    .map((segment) => segment.replace(/~1/g, "/").replace(/~0/g, "~"));
  if (!ROOTS.has(segments[0])) return call;
  let text = segments[0];
  for (const segment of segments.slice(1)) {
    if (SEGMENT.test(segment)) text += `.${segment}`;
    else if (INDEX.test(segment) && Number.isSafeInteger(Number(segment)))
      text += `[${segment}]`;
    else text += `[${JSON.stringify(segment)}]`;
  }
  return text;
}

function operand(node: unknown): string {
  const name = opName(node);
  const text = print(node);
  return name && (name in SYMBOLS || LOGICAL.has(name)) ? `(${text})` : text;
}

function link(chain: "and" | "or", node: unknown): string {
  const name = opName(node);
  const text = print(node);
  return name === chain || (chain === "and" && name === "or")
    ? `(${text})`
    : text;
}

function operation(op: unknown): string {
  if (!isRecord(op) || Object.keys(op).length !== 2) throw UNPRINTABLE;
  const { name, args } = op;
  if (typeof name !== "string" || !(name in ARITY) || !Array.isArray(args))
    throw UNPRINTABLE;
  if (name in SYMBOLS && args.length === 2)
    return `${operand(args[0])} ${SYMBOLS[name]} ${operand(args[1])}`;
  if ((name === "and" || name === "or") && args.length >= 2)
    return args.map((arg) => link(name, arg)).join(` ${name} `);
  if (name === "not" && args.length === 1) {
    const inner = opName(args[0]);
    const text = print(args[0]);
    return inner === "and" || inner === "or" ? `not (${text})` : `not ${text}`;
  }
  return `${name}(${args.map(print).join(", ")})`;
}

function print(node: unknown): string {
  if (!isRecord(node) || Object.keys(node).length !== 1) throw UNPRINTABLE;
  const [tag] = Object.keys(node);
  const body = node[tag];
  if (tag === "literal") return json(body);
  if (tag === "ref") {
    if (typeof body !== "string") throw UNPRINTABLE;
    return printRef(body);
  }
  if (tag === "array") {
    if (!Array.isArray(body)) throw UNPRINTABLE;
    const items = body.map(print).join(", ");
    return body.every(isLiteral) ? `array[${items}]` : `[${items}]`;
  }
  if (tag === "object") {
    if (!isRecord(body)) throw UNPRINTABLE;
    const keys = Object.keys(body);
    const entries = keys
      .map((key) => `${printKey(key)}: ${print(body[key])}`)
      .join(", ");
    return keys.every((key) => isLiteral(body[key]))
      ? `object{${entries}}`
      : `{${entries}}`;
  }
  if (tag === "op") return operation(body);
  throw UNPRINTABLE;
}

/** The canonical formula text of a tree, or null when the tree is outside the language. */
export function printFormula(expression: unknown): string | null {
  try {
    return print(expression);
  } catch (error) {
    if (error === UNPRINTABLE) return null;
    throw error;
  }
}
