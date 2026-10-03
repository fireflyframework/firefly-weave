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
// JSON values and RFC 6901 pointers for the schema-driven forms. No Angular
// imports: everything here runs in vitest under Node.
//
// Keys come from user schemas and pasted samples, so reads only see own
// properties and writes define properties instead of assigning them:
// "__proto__" and "constructor" are ordinary keys that never reach a
// prototype.

export type Json = null | boolean | number | string | Json[] | JsonObject;
export interface JsonObject {
  [key: string]: Json;
}
/** A pointer segment: a property name, or an array index (number or text). */
export type Segment = string | number;

export const isJsonObject = (value: unknown): value is JsonObject =>
  typeof value === "object" && value !== null && !Array.isArray(value);

export const hasOwn = (value: object, key: string): boolean =>
  Object.prototype.hasOwnProperty.call(value, key);

/** Escapes one reference token: "~" first, then "/". */
export const escapeSegment = (segment: string): string =>
  segment.replace(/~/g, "~0").replace(/\//g, "~1");
/** Unescapes one reference token: "~1" first, then "~0" (so "~01" is "~1"). */
export const unescapeSegment = (segment: string): string =>
  segment.replace(/~1/g, "/").replace(/~0/g, "~");

// The compiler's pointer grammar (compiler/expressions.py `pointer_segments`).
const POINTER = /^(?:\/(?:[^~/]|~[01])*)*$/;

/** True for "" (the whole document) and well-formed "/a/b~1c" pointers. */
export const isPointer = (text: string): boolean => POINTER.test(text);

/** Pointer text to unescaped segments, or null when it is not a pointer. */
export function parsePointer(pointer: string): string[] | null {
  if (!isPointer(pointer)) return null;
  return pointer === "" ? [] : pointer.slice(1).split("/").map(unescapeSegment);
}

/** Segments to pointer text; numbers become array indexes. */
export const formatPointer = (segments: readonly Segment[]): string =>
  segments.map((segment) => "/" + escapeSegment(String(segment))).join("");

/** The canonical array index a segment names, or -1 ("01", "-1" and "x" name none). */
function indexOf(segment: Segment): number {
  if (typeof segment === "number")
    return Number.isSafeInteger(segment) && segment >= 0 ? segment : -1;
  return /^(?:0|[1-9][0-9]*)$/.test(segment) && segment.length < 16
    ? Number(segment)
    : -1;
}

/** Reads the value at `segments`; undefined when any step is missing. */
export function getAt(value: unknown, segments: readonly Segment[]): unknown {
  let current: unknown = value;
  for (const segment of segments) {
    if (Array.isArray(current)) {
      const index = indexOf(segment);
      if (index < 0 || index >= current.length) return undefined;
      current = current[index];
    } else if (isJsonObject(current)) {
      const key = String(segment);
      if (!hasOwn(current, key)) return undefined;
      current = current[key];
    } else return undefined;
  }
  return current;
}

/** Copy of an object with one own data property defined (never assigned). */
function withKey(
  object: Record<string, unknown>,
  key: string,
  value: unknown,
): Record<string, unknown> {
  const next: Record<string, unknown> = {};
  for (const name of Object.keys(object))
    Object.defineProperty(next, name, {
      value: object[name],
      enumerable: true,
      writable: true,
      configurable: true,
    });
  Object.defineProperty(next, key, {
    value,
    enumerable: true,
    writable: true,
    configurable: true,
  });
  return next;
}
function withoutKey(
  object: Record<string, unknown>,
  key: string,
): Record<string, unknown> {
  const next: Record<string, unknown> = {};
  for (const name of Object.keys(object))
    if (name !== key)
      Object.defineProperty(next, name, {
        value: object[name],
        enumerable: true,
        writable: true,
        configurable: true,
      });
  return next;
}

/**
 * Returns a copy of `value` with `next` written at `segments`, sharing every
 * untouched subtree. Missing parents become objects; "-" or the length index
 * appends to an array. An empty path replaces the whole value.
 */
export function setAt(
  value: unknown,
  segments: readonly Segment[],
  next: unknown,
): unknown {
  if (!segments.length) return next;
  const [head, ...rest] = segments;
  if (Array.isArray(value)) {
    const index = head === "-" ? value.length : indexOf(head);
    if (index < 0 || index > value.length)
      throw RangeError(`No list item ${String(head)}`);
    const copy = value.slice();
    copy[index] = setAt(value[index], rest, next);
    return copy;
  }
  const object = isJsonObject(value) ? value : {};
  const key = String(head);
  const child = hasOwn(object, key) ? object[key] : undefined;
  return withKey(object, key, setAt(child, rest, next));
}

/**
 * Returns a copy of `value` without the member at `segments`. Objects the
 * removal leaves empty are removed too (an optional group the person cleared
 * is absent again), except the root itself. Missing paths return `value`.
 */
export function removeAt(
  value: unknown,
  segments: readonly Segment[],
): unknown {
  if (!segments.length || getAt(value, segments) === undefined) return value;
  const prune = (node: unknown, depth: number): unknown => {
    const segment = segments[depth];
    const last = depth === segments.length - 1;
    if (Array.isArray(node)) {
      const index = indexOf(segment);
      const copy = node.slice();
      if (last) copy.splice(index, 1);
      else copy[index] = prune(node[index], depth + 1);
      return copy;
    }
    const object = node as Record<string, unknown>;
    const key = String(segment);
    if (last) return withoutKey(object, key);
    const child = prune(object[key], depth + 1);
    return isJsonObject(child) && !Object.keys(child).length
      ? withoutKey(object, key)
      : withKey(object, key, child);
  };
  return prune(value, 0);
}

/** Stable text for a JSON value: object keys sorted, undefined members left out. */
export function canonicalJson(value: unknown): string {
  return JSON.stringify(value, (_key, member: unknown) => {
    if (!isJsonObject(member)) return member;
    const sorted: Record<string, unknown> = {};
    for (const key of Object.keys(member).sort())
      Object.defineProperty(sorted, key, {
        value: member[key],
        enumerable: true,
      });
    return sorted;
  });
}

/** Deep equality of JSON values; object key order does not matter. */
export const jsonEqual = (a: unknown, b: unknown): boolean =>
  canonicalJson(a) === canonicalJson(b);

export type ParsedJson =
  | { ok: true; value: Json }
  | { ok: false; message: string; line?: number; column?: number };

/**
 * Parses pasted JSON text. Failures carry a plain-language message, with the
 * line and column when the engine reports a position.
 */
export function parseJsonText(text: string): ParsedJson {
  if (!text.trim()) return { ok: false, message: "Paste a JSON value." };
  try {
    return { ok: true, value: JSON.parse(text) as Json };
  } catch {
    const offset = errorOffset(text);
    if (offset < 0)
      return {
        ok: false,
        message:
          "This is not valid JSON. Check for missing quotes, commas or brackets.",
      };
    const before = text.slice(0, offset).split("\n");
    const line = before.length;
    const column = before[before.length - 1].length + 1;
    return {
      ok: false,
      message: `This is not valid JSON near line ${line}, column ${column}. Check for missing quotes, commas or brackets.`,
      line,
      column,
    };
  }
}

/**
 * Offset of the first character that makes `text` invalid JSON (the text
 * length for a premature end), or -1 when no position is found. Engines word
 * and position their errors differently, so Studio finds it itself.
 */
function errorOffset(text: string): number {
  let at = 0;
  const fail = (): never => {
    throw at;
  };
  const space = () => {
    while (at < text.length && " \t\n\r".includes(text[at])) at++;
  };
  const expect = (word: string) => {
    for (const char of word) {
      if (text[at] !== char) fail();
      at++;
    }
  };
  const string = () => {
    at++;
    while (at < text.length && text[at] !== '"') {
      const code = text.charCodeAt(at);
      if (code < 0x20) fail();
      if (text[at] === "\\") {
        at++;
        if (text[at] === "u") {
          if (!/^[0-9a-fA-F]{4}$/.test(text.slice(at + 1, at + 5))) fail();
          at += 4;
        } else if (!'"\\/bfnrt'.includes(text[at] ?? "")) fail();
      }
      at++;
    }
    if (at >= text.length) fail();
    at++;
  };
  const number = () => {
    const match = /^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/.exec(
      text.slice(at),
    );
    if (!match) return fail();
    at += match[0].length;
  };
  const value = (depth: number): void => {
    if (depth > 512) fail();
    space();
    const char = text[at];
    if (char === "{") {
      at++;
      space();
      if (text[at] === "}") return void at++;
      for (;;) {
        space();
        if (text[at] !== '"') fail();
        string();
        space();
        expect(":");
        value(depth + 1);
        space();
        if (text[at] === ",") at++;
        else if (text[at] === "}") return void at++;
        else fail();
      }
    }
    if (char === "[") {
      at++;
      space();
      if (text[at] === "]") return void at++;
      for (;;) {
        value(depth + 1);
        space();
        if (text[at] === ",") at++;
        else if (text[at] === "]") return void at++;
        else fail();
      }
    }
    if (char === '"') return string();
    if (char === "t") return expect("true");
    if (char === "f") return expect("false");
    if (char === "n") return expect("null");
    number();
  };
  try {
    value(0);
    space();
    return at < text.length ? at : -1;
  } catch (offset) {
    return typeof offset === "number" ? Math.min(offset, text.length) : -1;
  }
}
