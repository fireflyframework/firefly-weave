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
// The address a Call an API step calls, built as the HTTP connector builds
// it: each {name} filled with its value as text and encoded with
// quote(text, safe="-._~"), then the query parameters in order (a list
// parameter repeats its name). A value the connector would refuse (another
// type than the parameter's, a list where it takes one value, text it
// doesn't send, an address over 16,384 bytes) gives the reason instead.
// Locally the connection's address reads "Connection address", and the
// address limit counts the path and query only. Headers and the body are
// never shown.
import type { Json } from "../../../forms/core/json";
import type { PreviewResult } from "../registry";
import { urlParts } from "./url-template";

/** A path segment or query part, encoded as the connector's quote(text, safe="-._~"). */
export function encodeSegment(text: string): string {
  return encodeURIComponent(text).replace(
    /[!'()*]/g,
    (character) => `%${character.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

/** How the action declares a parameter. */
export interface UrlParameter {
  type: "string" | "integer" | "boolean";
  array: boolean;
}
/** Studio's own actions declare every path and query parameter as one text value. */
const TEXT: UrlParameter = { type: "string", array: false };

/** The address, or why the connector would refuse it; null while a value doesn't compute. */
export type UrlPreview = { address: string } | { refused: string };

const TYPE_WORDS: Record<UrlParameter["type"], string> = {
  string: "text",
  integer: "a whole number",
  boolean: "Yes/No",
};
function kindOf(value: Json): string {
  if (value === null) return "empty";
  if (typeof value === "string") return "text";
  if (typeof value === "boolean") return "Yes/No";
  if (typeof value === "number")
    return Number.isSafeInteger(value) ? "a whole number" : "a number";
  return Array.isArray(value) ? "a list" : "a group";
}

class Refused {
  constructor(readonly reason: string) {}
}

/** A value as the connector writes it in an address (its `_scalar`). */
function scalar(value: Json, parameter: UrlParameter, label: string): string {
  const kind = kindOf(value);
  if (kind !== TYPE_WORDS[parameter.type])
    throw new Refused(
      `${label} must be ${TYPE_WORDS[parameter.type]}, not ${kind}`,
    );
  if (typeof value === "string") {
    if (value.length > 4096)
      throw new Refused(`${label} is longer than 4,096 characters`);
    if (/[\u0000-\u001f\u007f]/.test(value))
      throw new Refused(`${label} has a control character`);
    return value;
  }
  return String(value);
}

/** Text JavaScript can't encode (a lone surrogate) has no address. */
class Unshown {}
function encoded(text: string): string {
  try {
    return encodeSegment(text);
  } catch {
    throw new Unshown();
  }
}

export function urlPreview(
  path: string,
  values: ReadonlyMap<string, PreviewResult | null>,
  query: readonly { name: string; preview: PreviewResult | null }[],
  parameter: (location: "path" | "query", name: string) => UrlParameter = () =>
    TEXT,
  prefix = "Connection address",
): UrlPreview | null {
  const parts = urlParts(path);
  // Only values that compute can be judged; until then, nothing.
  for (const part of parts)
    if (!("text" in part) && !values.get(part.name)?.ok) return null;
  if (query.some((item) => !item.preview?.ok)) return null;
  try {
    let address = "";
    for (const part of parts) {
      if ("text" in part) {
        address += part.text;
        continue;
      }
      const result = values.get(part.name) as { value?: Json };
      const label = `{${part.name}}`;
      const text = scalar(
        result.value ?? null,
        parameter("path", part.name),
        label,
      );
      if (text === "" || text === "." || text === "..")
        throw new Refused(`${label} can't be empty, "." or ".."`);
      address += encoded(text);
    }
    const pairs: string[] = [];
    for (const item of query) {
      const value = (item.preview as { value?: Json }).value ?? null;
      const declared = parameter("query", item.name);
      let items: Json[];
      if (declared.array) {
        if (!Array.isArray(value))
          throw new Refused(`${item.name} takes a list`);
        if (value.length > 100)
          throw new Refused(`${item.name} has more than 100 items`);
        items = value;
      } else {
        if (Array.isArray(value))
          throw new Refused(`${item.name} takes one value, not a list`);
        items = [value];
      }
      const name = encoded(item.name);
      for (const each of items)
        pairs.push(`${name}=${encoded(scalar(each, declared, item.name))}`);
    }
    const tail = `${address}${pairs.length ? `?${pairs.join("&")}` : ""}`;
    if (new TextEncoder().encode(tail).length > 16384)
      throw new Refused("the address is longer than 16,384 bytes");
    return { address: `${prefix}${tail}` };
  } catch (error) {
    if (error instanceof Refused) return { refused: error.reason };
    if (error instanceof Unshown) return null;
    throw error;
  }
}
