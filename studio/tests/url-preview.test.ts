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
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import {
  encodeSegment,
  urlPreview,
} from "../src/app/editor/ndv/params/url-preview";

const ok = (value: unknown) => ({ ok: true, value }) as const;
const waiting = {
  ok: false,
  error: { code: "WV-PREVIEW-WAITING", message: "…" },
} as const;

describe("the full address of Call an API", () => {
  it("encodes a path segment exactly as the HTTP connector does", () => {
    const { pathSegments } = JSON.parse(
      readFileSync(
        resolve(import.meta.dirname, "fixtures/expression-corpus.json"),
        "utf8",
      ),
    ) as { pathSegments: { text: string; encoded: string }[] };
    expect(pathSegments.length).toBeGreaterThan(10);
    for (const { text, encoded } of pathSegments)
      expect(encodeSegment(text), text).toBe(encoded);
  });
  it("fills each placeholder and adds the query", () => {
    expect(
      urlPreview("/customers/{id}/orders", new Map([["id", ok("c 1042")]]), [
        { name: "status", preview: ok("open") },
        { name: "sort", preview: ok("-date") },
      ]),
    ).toEqual({
      address:
        "Connection address/customers/c%201042/orders?status=open&sort=-date",
    });
  });
  it("refuses a value of another type than the parameter's, as the connector does", () => {
    // Studio's own actions declare every parameter as text.
    expect(urlPreview("/orders/{id}", new Map([["id", ok(1042)]]), [])).toEqual(
      { refused: "{id} must be text, not a whole number" },
    );
    expect(
      urlPreview("/orders", new Map(), [{ name: "all", preview: ok(true) }]),
    ).toEqual({ refused: "all must be text, not Yes/No" });
    expect(
      urlPreview("/orders", new Map(), [{ name: "q", preview: ok(null) }]),
    ).toEqual({ refused: "q must be text, not empty" });
    expect(
      urlPreview("/customers/{id}", new Map([["id", ok({ a: 1 })]]), []),
    ).toEqual({ refused: "{id} must be text, not a group" });
    expect(
      urlPreview("/customers/{id}", new Map([["id", ok(1.5)]]), []),
    ).toEqual({ refused: "{id} must be text, not a number" });
    // A parameter declared as a whole number or Yes/No takes those.
    const typed = (location: "path" | "query", name: string) =>
      name === "limit"
        ? { type: "integer" as const, array: false }
        : name === "all"
          ? { type: "boolean" as const, array: false }
          : { type: "string" as const, array: location === "query" };
    expect(
      urlPreview(
        "/orders",
        new Map(),
        [
          { name: "limit", preview: ok(25) },
          { name: "all", preview: ok(false) },
          { name: "tag", preview: ok(["a b", "c"]) },
        ],
        typed,
      ),
    ).toEqual({
      address: "Connection address/orders?limit=25&all=false&tag=a%20b&tag=c",
    });
    expect(
      urlPreview(
        "/orders",
        new Map(),
        [{ name: "limit", preview: ok(true) }],
        typed,
      ),
    ).toEqual({ refused: "limit must be a whole number, not Yes/No" });
    expect(
      urlPreview(
        "/orders",
        new Map(),
        [{ name: "tag", preview: ok("a") }],
        typed,
      ),
    ).toEqual({ refused: "tag takes a list" });
    expect(
      urlPreview(
        "/orders",
        new Map(),
        [{ name: "tag", preview: ok(Array.from({ length: 101 }, () => "a")) }],
        typed,
      ),
    ).toEqual({ refused: "tag has more than 100 items" });
  });
  it("refuses a list where the parameter takes one value", () => {
    expect(
      urlPreview("/orders", new Map(), [
        { name: "tag", preview: ok(["a", "c"]) },
      ]),
    ).toEqual({ refused: "tag takes one value, not a list" });
  });
  it("refuses text the connector refuses", () => {
    expect(
      urlPreview("/orders/{id}", new Map([["id", ok("a\nb")]]), []),
    ).toEqual({ refused: "{id} has a control character" });
    expect(
      urlPreview("/orders/{id}", new Map([["id", ok("x".repeat(4097))]]), []),
    ).toEqual({ refused: "{id} is longer than 4,096 characters" });
    for (const segment of ["", ".", ".."])
      expect(
        urlPreview("/customers/{id}", new Map([["id", ok(segment)]]), []),
      ).toEqual({ refused: '{id} can\'t be empty, "." or ".."' });
  });
  it("refuses an address longer than the connector's 16,384 bytes", () => {
    const long = "é".repeat(4000); // 24,000 bytes encoded
    expect(urlPreview("/a/{b}", new Map([["b", ok(long)]]), [])).toEqual({
      refused: "the address is longer than 16,384 bytes",
    });
    const fits = "a".repeat(4000);
    expect(urlPreview("/a/{b}", new Map([["b", ok(fits)]]), [])).toEqual({
      address: `Connection address/a/${fits}`,
    });
  });
  it("shows nothing until every value computes", () => {
    expect(
      urlPreview("/customers/{id}", new Map([["id", waiting]]), []),
    ).toBeNull();
    expect(
      urlPreview("/customers", new Map(), [{ name: "q", preview: null }]),
    ).toBeNull();
  });
});
