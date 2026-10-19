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
// The platform's expression evaluator (compiler/expressions.py `evaluate`),
// ported line by line so Studio can preview a formula without asking anyone.
// It interprets JSON trees only: no formula text ever runs as code. Python
// counts text in code points and sizes in UTF-8 bytes of compact JSON, and
// writes floats with str(); so does this port. Studio reads JSON numbers as
// JavaScript does and can't tell 2 from 2.0: where that could change a
// result, it answers AMBIGUOUS instead of guessing.
import type { Json } from "../../forms/core/json";

export interface Limits {
  maxDepth: number;
  maxExpressionNodes: number;
  maxDocumentNodes: number;
  maxPayloadBytes: number;
}
export const DEFAULT_LIMITS: Readonly<Limits> = Object.freeze({
  maxDepth: 32,
  maxExpressionNodes: 10_000,
  maxDocumentNodes: 100_000,
  maxPayloadBytes: 1_048_576,
});
export const MAX_SAFE_INTEGER = 9_007_199_254_740_991;
/** Studio can't tell what the platform would answer (see the file comment). */
export const AMBIGUOUS = "AMBIGUOUS";

export interface EvalFailure {
  ok: false;
  /** WV-EXPR-<CODE>, as the platform names it, or AMBIGUOUS. */
  code: string;
  /** Where in the expression document, as an RFC 6901 pointer. */
  path: string;
  /** The JSON types of the operands at a WV-EXPR-TYPE. */
  types?: string[];
}
export type EvalResult = { ok: true; value: Json } | EvalFailure;

class Failure extends Error {
  constructor(
    readonly code: string,
    readonly path: string,
    readonly types?: string[],
  ) {
    super(code);
  }
}
const failure = (code: string, path: string, types?: string[]) =>
  new Failure(code === AMBIGUOUS ? code : `WV-EXPR-${code}`, path, types);

class Work {
  constructor(private remaining: number) {}
  charge(path: string, amount = 1): void {
    this.remaining -= amount;
    if (this.remaining < 0) throw failure("RESOURCE_LIMIT", path);
  }
}

const MISSING: unique symbol = Symbol("missing");
type Value = Json | typeof MISSING;
type Node = Record<string, unknown>;

const COMPARISONS = new Set(["eq", "ne", "lt", "lte", "gt", "gte"]);
const COLLECTIONS = new Set([
  "contains",
  "notContains",
  "in",
  "notIn",
  "startsWith",
  "endsWith",
]);
const OPERATORS = new Set([
  ...COMPARISONS,
  ...COLLECTIONS,
  "concat",
  "join",
  "and",
  "or",
  "not",
  "exists",
  "coalesce",
]);
const POINTER = /^(?:\/(?:[^~/]|~[01])*)*$/;
const INDEX = /^(?:0|[1-9][0-9]*)$/;
const LONE_SURROGATE =
  /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/;

const isRecord = (value: unknown): value is Node =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const own = (value: object, key: string): boolean =>
  Object.prototype.hasOwnProperty.call(value, key);
/** Sets a key as an own property, even "__proto__". */
function put(target: Record<string, Json>, key: string, value: Json): void {
  Object.defineProperty(target, key, {
    value,
    enumerable: true,
    writable: true,
    configurable: true,
  });
}

export function jsonType(value: Json): string {
  if (value === null) return "null";
  if (Array.isArray(value)) return "array";
  if (typeof value === "number")
    return Number.isInteger(value) ? "integer" : "number";
  return typeof value === "object" ? "object" : typeof value;
}

/** Python's len(): code points, a lone surrogate counting as one. */
function codePoints(text: string): number {
  let count = 0;
  for (let i = 0; i < text.length; i++) {
    const unit = text.charCodeAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff && i + 1 < text.length) {
      const next = text.charCodeAt(i + 1);
      if (next >= 0xdc00 && next <= 0xdfff) i++;
    }
    count++;
  }
  return count;
}

/** Compares by Unicode code point, as Python compares str. */
export function comparePoints(a: string, b: string): number {
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    const x = a.codePointAt(i)!;
    const y = b.codePointAt(j)!;
    if (x !== y) return x < y ? -1 : 1;
    i += x > 0xffff ? 2 : 1;
    j += y > 0xffff ? 2 : 1;
  }
  if (i < a.length) return 1;
  return j < b.length ? -1 : 0;
}

/** Python's str() of a finite float that is not a whole number. */
export function pythonFloatText(value: number): string {
  const [mantissa, exponentText] = value.toExponential().split("e");
  const exponent = Number(exponentText);
  const negative = mantissa.startsWith("-");
  const digits = mantissa.replace("-", "").replace(".", "");
  const sign = negative ? "-" : "";
  if (exponent < -4 || exponent >= 16) {
    const body = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
    const power = String(Math.abs(exponent)).padStart(2, "0");
    return `${sign}${body}e${exponent < 0 ? "-" : "+"}${power}`;
  }
  if (exponent < 0) return `${sign}0.${"0".repeat(-exponent - 1)}${digits}`;
  if (digits.length > exponent + 1)
    return `${sign}${digits.slice(0, exponent + 1)}.${digits.slice(exponent + 1)}`;
  return `${sign}${digits}${"0".repeat(exponent + 1 - digits.length)}.0`;
}

/** The text concat and join write for a value; null for any other type. */
export function textOf(value: Json): string | null {
  if (typeof value === "string") return value;
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return String(value);
  return null;
}

export function pointerSegments(pointer: unknown, path = ""): string[] {
  if (typeof pointer !== "string" || !POINTER.test(pointer))
    throw failure("INVALID", path);
  return pointer
    .split("/")
    .slice(1)
    .map((segment) => segment.replace(/~1/g, "/").replace(/~0/g, "~"));
}

const pointerChild = (parent: string, key: string): string =>
  `${parent}/${key.replace(/~/g, "~0").replace(/\//g, "~1")}`;

function arrayIndex(segment: string, length: number): number | null {
  if (!INDEX.test(segment) || segment.length > 15) return null;
  const index = Number(segment);
  return index < length ? index : null;
}

function resolvePointer(context: Json, segments: string[]): Value {
  let value: Json = context;
  for (const segment of segments) {
    if (isRecord(value)) {
      if (!own(value, segment)) return MISSING;
      value = value[segment] as Json;
    } else if (Array.isArray(value)) {
      const index = arrayIndex(segment, value.length);
      if (index === null) return MISSING;
      value = value[index];
    } else return MISSING;
  }
  return value;
}

/** Bytes of a string as compact JSON in UTF-8, as Python's json.dumps writes it. */
function jsonStringBytes(value: string): number {
  let size = 2;
  for (const character of value) {
    const code = character.codePointAt(0)!;
    if (character === '"' || character === "\\") size += 2;
    else if (
      code === 8 ||
      code === 9 ||
      code === 10 ||
      code === 12 ||
      code === 13
    )
      size += 2;
    else if (code < 0x20) size += 6;
    else if (code < 0x80) size += 1;
    else if (code < 0x800) size += 2;
    else if (code < 0x10000) size += 3;
    else size += 4;
  }
  return size;
}

function stringSize(value: string, limits: Limits, path: string): number {
  if (
    value.length > limits.maxPayloadBytes &&
    codePoints(value) > limits.maxPayloadBytes
  )
    throw failure("RESOURCE_LIMIT", path);
  if (LONE_SURROGATE.test(value)) throw failure("INVALID_JSON", path);
  const size = jsonStringBytes(value);
  if (size > limits.maxPayloadBytes) throw failure("RESOURCE_LIMIT", path);
  return size;
}

interface Measured {
  size: number;
  /**
   * Bytes the value could gain if its whole numbers were floats: two for
   * each (1500 or 1500.0), three for a negative zero (0 or -0.0).
   */
  slack: number;
}
/** Bytes a whole number could gain if it were a float. */
const slackOf = (value: number): number => (Object.is(value, -0) ? 3 : 2);

/**
 * Runs `walk` for a value measured at `path` once the value may already be
 * over the byte budget when its whole numbers are floats (`over()`). Read
 * as floats, the platform stops there with RESOURCE_LIMIT at `path`; read
 * as whole numbers, it goes on. Only when it goes on to that same failure is
 * the answer certain; any other ending is AMBIGUOUS.
 */
function settled<T>(path: string, over: () => boolean, walk: () => T): T {
  let result: T;
  try {
    result = walk();
  } catch (error) {
    if (
      over() &&
      !(
        error instanceof Failure &&
        error.code === "WV-EXPR-RESOURCE_LIMIT" &&
        error.path === path
      )
    )
      throw failure(AMBIGUOUS, path);
    throw error;
  }
  if (over()) throw failure(AMBIGUOUS, path);
  return result;
}

/** compiler/expressions.py `measure_value`: bounded bytes, nodes and depth. */
function measure(
  value: unknown,
  limits: Limits,
  path: string,
  context = false,
  work?: Work,
): Measured {
  let size = 0;
  let nodes = 0;
  let slack = 0;
  let over = false;
  const active = new Set<object>();
  const stack: [unknown, number, boolean][] = [[value, 0, false]];
  return settled(
    path,
    () => over,
    () => {
      while (stack.length) {
        const [item, depth, leaving] = stack.pop()!;
        if (leaving) {
          active.delete(item as object);
          continue;
        }
        nodes += 1;
        work?.charge(path);
        if (nodes > limits.maxDocumentNodes)
          throw failure("RESOURCE_LIMIT", path);
        if (Array.isArray(item) || isRecord(item)) {
          if (active.has(item)) throw failure("INVALID_JSON", path);
          if (depth + 1 > limits.maxDepth)
            throw failure("RESOURCE_LIMIT", path);
          active.add(item);
          stack.push([item, depth, true]);
          const keys = Array.isArray(item) ? null : Object.keys(item);
          const length = Array.isArray(item) ? item.length : keys!.length;
          size += 2 + Math.max(0, length - 1);
          if (length > limits.maxDocumentNodes - nodes)
            throw failure("RESOURCE_LIMIT", path);
          if (Array.isArray(item))
            for (const child of item) stack.push([child, depth + 1, false]);
          else
            for (const key of keys!) {
              size += stringSize(key, limits, path) + 1;
              stack.push([(item as Node)[key], depth + 1, false]);
            }
        } else if (typeof item === "string")
          size += stringSize(item, limits, path);
        else if (item === null) size += 4;
        else if (typeof item === "boolean") size += item ? 4 : 5;
        else if (typeof item === "number") {
          if (!Number.isFinite(item)) throw failure("INVALID_JSON", path);
          if (Number.isInteger(item)) {
            if (Math.abs(item) > MAX_SAFE_INTEGER)
              throw failure(AMBIGUOUS, path);
            size += String(item).length;
            slack += slackOf(item);
          } else size += pythonFloatText(item).length;
        } else throw failure("INVALID_JSON", path);
        if (!context && size > limits.maxPayloadBytes)
          throw failure("RESOURCE_LIMIT", path);
        // Read as floats, the value may already be over the budget here.
        if (!context && size + slack > limits.maxPayloadBytes) over = true;
      }
      return { size, slack };
    },
  );
}

function childrenOf(
  expression: unknown,
  path: string,
  limits: Limits,
  work: Work,
): [unknown, string][] {
  if (!isRecord(expression) || Object.keys(expression).length !== 1)
    throw failure("INVALID", path);
  const [tag, value] = Object.entries(expression)[0];
  const tagPath = pointerChild(path, tag);
  if (tag === "literal") {
    measure(value, limits, tagPath, false, work);
    return [];
  }
  if (tag === "ref") {
    if (typeof value !== "string") throw failure("INVALID", tagPath);
    stringSize(value, limits, tagPath);
    pointerSegments(value, tagPath);
    return [];
  }
  if (tag === "object" && isRecord(value)) {
    const keys = Object.keys(value);
    if (keys.length > limits.maxExpressionNodes)
      throw failure("RESOURCE_LIMIT", tagPath);
    return keys.map((key) => {
      stringSize(key, limits, tagPath);
      return [value[key], pointerChild(tagPath, key)];
    });
  }
  if (tag === "array" && Array.isArray(value)) {
    if (value.length > limits.maxExpressionNodes)
      throw failure("RESOURCE_LIMIT", tagPath);
    return value.map((child, i) => [child, pointerChild(tagPath, String(i))]);
  }
  if (tag !== "op" || !isRecord(value)) throw failure("INVALID", tagPath);
  const keys = Object.keys(value);
  if (keys.length !== 2 || !own(value, "name") || !own(value, "args"))
    throw failure("INVALID", tagPath);
  const { name, args } = value as { name: unknown; args: unknown };
  if (typeof name !== "string" || !OPERATORS.has(name) || !Array.isArray(args))
    throw failure("INVALID", tagPath);
  if (args.length > limits.maxExpressionNodes)
    throw failure("RESOURCE_LIMIT", tagPath);
  const count = args.length;
  const first = args[0];
  if (
    ((COMPARISONS.has(name) || COLLECTIONS.has(name) || name === "join") &&
      count !== 2) ||
    ((name === "not" || name === "exists") && count !== 1) ||
    ((name === "and" ||
      name === "or" ||
      name === "coalesce" ||
      name === "concat") &&
      count < 1) ||
    (name === "exists" &&
      (!isRecord(first) ||
        Object.keys(first).length !== 1 ||
        !own(first, "ref")))
  )
    throw failure("ARITY", tagPath);
  return args.map((child, i) => [
    child,
    pointerChild(`${tagPath}/args`, String(i)),
  ]);
}

/** compiler/expressions.py `count_expression_nodes`: structure and budgets before anything runs. */
export function countExpressionNodes(
  expression: unknown,
  limits: Limits = DEFAULT_LIMITS,
  initial = 0,
  path = "",
): number {
  let count = initial;
  const work = new Work(limits.maxDocumentNodes);
  const stack: [unknown, string, number][] = [[expression, path, 1]];
  while (stack.length) {
    const [node, nodePath, depth] = stack.pop()!;
    count += 1;
    if (count > limits.maxExpressionNodes || depth > limits.maxDepth)
      throw failure("RESOURCE_LIMIT", nodePath);
    const children = childrenOf(node, nodePath, limits, work);
    if (children.length > limits.maxExpressionNodes - count)
      throw failure("RESOURCE_LIMIT", nodePath);
    for (let i = children.length - 1; i >= 0; i--)
      stack.push([children[i][0], children[i][1], depth + 1]);
  }
  return count;
}

function jsonEqual(
  left: Json,
  right: Json,
  work?: (left: Json, right: Json) => void,
): boolean {
  work?.(left, right);
  if (typeof left === "boolean" || typeof right === "boolean")
    return typeof left === typeof right && left === right;
  if (isRecord(left) && isRecord(right)) {
    const keys = Object.keys(left);
    if (
      keys.length !== Object.keys(right).length ||
      !keys.every((key) => own(right, key))
    )
      return false;
    return keys.every((key) =>
      jsonEqual(left[key] as Json, right[key] as Json, work),
    );
  }
  if (Array.isArray(left) && Array.isArray(right))
    return (
      left.length === right.length &&
      left.every((item, i) => jsonEqual(item, right[i], work))
    );
  if (
    isRecord(left) ||
    isRecord(right) ||
    Array.isArray(left) ||
    Array.isArray(right)
  )
    return false;
  return left === right;
}

class Evaluator {
  private operations = 0;
  private readonly valueWork: Work;
  private readonly stringWork: Work;
  constructor(
    private readonly context: Json,
    private readonly limits: Limits,
  ) {
    this.valueWork = new Work(limits.maxDocumentNodes);
    this.stringWork = new Work(limits.maxPayloadBytes);
  }

  private comparisonWork(left: Json, right: Json, path: string): void {
    this.valueWork.charge(path);
    if (typeof left === "string" && typeof right === "string")
      this.stringWork.charge(path, codePoints(left) + codePoints(right));
    else if (isRecord(left) && isRecord(right)) {
      const leftKeys = Object.keys(left);
      const rightKeys = Object.keys(right);
      this.valueWork.charge(path, leftKeys.length + rightKeys.length);
      this.stringWork.charge(
        path,
        [...leftKeys, ...rightKeys].reduce(
          (sum, key) => sum + codePoints(key),
          0,
        ),
      );
    }
  }

  private text(value: Json, path: string): string {
    const text = textOf(value);
    if (text === null) throw failure("TYPE", path, [jsonType(value)]);
    this.stringWork.charge(path, codePoints(text));
    return text;
  }

  private collection(name: string, left: Json, right: Json, path: string) {
    const [container, needle] =
      name === "in" || name === "notIn" ? [right, left] : [left, right];
    let matched: boolean;
    if (
      name === "startsWith" ||
      name === "endsWith" ||
      (typeof container === "string" &&
        (name === "contains" || name === "notContains"))
    ) {
      if (typeof container !== "string" || typeof needle !== "string")
        throw failure("TYPE", path, [jsonType(container), jsonType(needle)]);
      this.comparisonWork(container, needle, path);
      if (name === "startsWith") return container.startsWith(needle);
      if (name === "endsWith") return container.endsWith(needle);
      matched = container.includes(needle);
    } else if (Array.isArray(container))
      matched = container.some((item) =>
        jsonEqual(item, needle, (a, b) => this.comparisonWork(a, b, path)),
      );
    else throw failure("TYPE", path, [jsonType(container)]);
    return name === "notContains" || name === "notIn" ? !matched : matched;
  }

  run(
    expression: Node,
    path: string,
    allowMissing = false,
    presenceOnly = false,
  ): Value {
    this.operations += 1;
    if (this.operations > this.limits.maxExpressionNodes)
      throw failure("RESOURCE_LIMIT", path);
    const [tag, body] = Object.entries(expression)[0];
    let result: Json;
    if (tag === "literal") result = body as Json;
    else if (tag === "ref") {
      const found = resolvePointer(this.context, pointerSegments(body));
      if (found === MISSING) {
        if (allowMissing) return found;
        throw failure("MISSING", path);
      }
      if (presenceOnly) return found;
      result = found;
    } else if (tag === "object") {
      const built: Record<string, Json> = {};
      let size = 2;
      let slack = 0;
      let first = true;
      let over = false;
      settled(
        path,
        () => over,
        () => {
          for (const [key, child] of Object.entries(body as Node)) {
            const value = this.run(
              child as Node,
              pointerChild(`${path}/object`, key),
            ) as Json;
            const keySize = stringSize(key, this.limits, path);
            const measured = measure(
              value,
              this.limits,
              path,
              false,
              this.valueWork,
            );
            size += keySize + 1 + measured.size + (first ? 0 : 1);
            slack += measured.slack;
            if (size > this.limits.maxPayloadBytes)
              throw failure("RESOURCE_LIMIT", path);
            if (size + slack > this.limits.maxPayloadBytes) over = true;
            put(built, key, value);
            first = false;
          }
        },
      );
      result = built;
    } else if (tag === "array") {
      const built: Json[] = [];
      let size = 2;
      let slack = 0;
      let over = false;
      settled(
        path,
        () => over,
        () =>
          (body as Node[]).forEach((child, i) => {
            const value = this.run(child, `${path}/array/${i}`) as Json;
            const measured = measure(
              value,
              this.limits,
              path,
              false,
              this.valueWork,
            );
            size += measured.size + (i > 0 ? 1 : 0);
            slack += measured.slack;
            if (size > this.limits.maxPayloadBytes)
              throw failure("RESOURCE_LIMIT", path);
            if (size + slack > this.limits.maxPayloadBytes) over = true;
            built.push(value);
          }),
      );
      result = built;
    } else {
      const { name, args } = body as { name: string; args: Node[] };
      const at = `${path}/op/args`;
      if (name === "exists")
        result = this.run(args[0], `${at}/0`, true, true) !== MISSING;
      else if (name === "coalesce") {
        result = null;
        for (let i = 0; i < args.length; i++) {
          const candidate = this.run(args[i], `${at}/${i}`, true);
          if (candidate !== MISSING && candidate !== null) {
            result = candidate;
            break;
          }
        }
      } else if (name === "and" || name === "or") {
        result = name === "and";
        for (let i = 0; i < args.length; i++) {
          const value = this.run(args[i], `${at}/${i}`) as Json;
          if (typeof value !== "boolean")
            throw failure("TYPE", `${at}/${i}`, [jsonType(value)]);
          result = value;
          if (value === (name === "or")) break;
        }
      } else if (name === "not") {
        const value = this.run(args[0], `${at}/0`) as Json;
        if (typeof value !== "boolean")
          throw failure("TYPE", `${at}/0`, [jsonType(value)]);
        result = !value;
      } else if (name === "concat") {
        let text = "";
        for (let i = 0; i < args.length; i++)
          text += this.text(
            this.run(args[i], `${at}/${i}`) as Json,
            `${at}/${i}`,
          );
        result = text;
      } else if (name === "join") {
        const items = this.run(args[0], `${at}/0`) as Json;
        const separator = this.run(args[1], `${at}/1`) as Json;
        if (!Array.isArray(items))
          throw failure("TYPE", `${at}/0`, [jsonType(items)]);
        if (typeof separator !== "string")
          throw failure("TYPE", `${at}/1`, [jsonType(separator)]);
        // A list item that isn't text names the list first, so the words
        // can tell it from a value that isn't a list.
        const texts = items.map((item) => {
          if (textOf(item) === null)
            throw failure("TYPE", `${at}/0`, ["array", jsonType(item)]);
          return this.text(item, `${at}/0`);
        });
        this.stringWork.charge(
          path,
          codePoints(separator) * Math.max(texts.length - 1, 0),
        );
        result = texts.join(separator);
      } else {
        const left = this.run(args[0], `${at}/0`) as Json;
        const right = this.run(args[1], `${at}/1`) as Json;
        if (name === "eq" || name === "ne") {
          const equal = jsonEqual(left, right);
          result = name === "eq" ? equal : !equal;
        } else if (COLLECTIONS.has(name))
          result = this.collection(name, left, right, path);
        else {
          const numbers = typeof left === "number" && typeof right === "number";
          const strings = typeof left === "string" && typeof right === "string";
          if (!numbers && !strings)
            throw failure("TYPE", path, [jsonType(left), jsonType(right)]);
          const order = numbers
            ? Math.sign((left as number) - (right as number))
            : comparePoints(left as string, right as string);
          result =
            name === "lt"
              ? order < 0
              : name === "lte"
                ? order <= 0
                : name === "gt"
                  ? order > 0
                  : order >= 0;
        }
      }
    }
    measure(result, this.limits, path, false, this.valueWork);
    return result;
  }
}

/** A copy that owns its keys, even "__proto__". */
function copy(value: Json): Json {
  if (Array.isArray(value)) return value.map(copy);
  if (!isRecord(value)) return value;
  const out: Record<string, Json> = {};
  for (const key of Object.keys(value)) put(out, key, copy(value[key] as Json));
  return out;
}

/**
 * compiler/expressions.py `evaluate`: the expression's structure and budgets
 * first, then the context's, then the value. Never throws for JSON input.
 */
export function evaluate(
  expression: unknown,
  context: unknown,
  limits: Limits = DEFAULT_LIMITS,
): EvalResult {
  try {
    countExpressionNodes(expression, limits);
    if (!isRecord(context)) throw failure("INVALID_JSON", "");
    measure(context, limits, "", true);
    const value = new Evaluator(context as Json, limits).run(
      expression as Node,
      "",
    ) as Json;
    return { ok: true, value: copy(value) };
  } catch (error) {
    if (!(error instanceof Failure)) throw error;
    return error.types
      ? { ok: false, code: error.code, path: error.path, types: error.types }
      : { ok: false, code: error.code, path: error.path };
  }
}
