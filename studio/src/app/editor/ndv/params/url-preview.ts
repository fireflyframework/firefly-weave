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
// it: each {name} filled with its value as text (a string, a whole number,
// or true/false) and encoded with quote(text, safe="-._~"), then the query
// parameters in order (a list repeats its name). Locally the connection's
// address reads "Connection address". Headers and the body are never shown.
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

/** A value as the connector writes it in an address, or null when the connector refuses it. */
function scalar(value: Json): string | null {
  if (typeof value === "string")
    return value.length > 4096 || /[\u0000-\u001f\u007f]/.test(value)
      ? null
      : value;
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number" && Number.isSafeInteger(value))
    return String(value);
  return null;
}

function encoded(text: string | null): string | null {
  if (text === null) return null;
  try {
    return encodeSegment(text);
  } catch {
    // Text JavaScript can't encode (a lone surrogate): no address.
    return null;
  }
}

export function urlPreview(
  path: string,
  values: ReadonlyMap<string, PreviewResult | null>,
  query: readonly { name: string; preview: PreviewResult | null }[],
  prefix = "Connection address",
): string | null {
  let address = "";
  for (const part of urlParts(path)) {
    if ("text" in part) {
      address += part.text;
      continue;
    }
    const result = values.get(part.name);
    const text = result?.ok ? scalar(result.value ?? null) : null;
    if (text === "" || text === "." || text === "..") return null;
    const segment = encoded(text);
    if (segment === null) return null;
    address += segment;
  }
  const pairs: string[] = [];
  for (const item of query) {
    if (!item.preview?.ok) return null;
    const value = item.preview.value ?? null;
    const items = Array.isArray(value) ? value : [value];
    if (items.length > 100) return null;
    const name = encoded(item.name);
    if (name === null) return null;
    for (const each of items) {
      const text = encoded(scalar(each));
      if (text === null) return null;
      pairs.push(`${name}=${text}`);
    }
  }
  return `${prefix}${address}${pairs.length ? `?${pairs.join("&")}` : ""}`;
}
