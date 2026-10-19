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
        { name: "limit", preview: ok(25) },
        { name: "all", preview: ok(true) },
      ]),
    ).toBe(
      "Connection address/customers/c%201042/orders?status=open&limit=25&all=true",
    );
  });
  it("repeats a list's name in the query and refuses what the connector refuses", () => {
    expect(
      urlPreview("/orders", new Map(), [
        { name: "tag", preview: ok(["a b", "c"]) },
      ]),
    ).toBe("Connection address/orders?tag=a%20b&tag=c");
    expect(
      urlPreview("/orders/{id}", new Map([["id", ok("a\nb")]]), []),
    ).toBeNull();
    expect(
      urlPreview("/orders/{id}", new Map([["id", ok("x".repeat(4097))]]), []),
    ).toBeNull();
    expect(
      urlPreview("/orders", new Map(), [{ name: "q", preview: ok(null) }]),
    ).toBeNull();
  });
  it("shows nothing until every value computes and fits a path", () => {
    expect(
      urlPreview("/customers/{id}", new Map([["id", waiting]]), []),
    ).toBeNull();
    expect(
      urlPreview("/customers/{id}", new Map([["id", ok({ a: 1 })]]), []),
    ).toBeNull();
    expect(
      urlPreview("/customers/{id}", new Map([["id", ok("..")]]), []),
    ).toBeNull();
    expect(
      urlPreview("/customers/{id}", new Map([["id", ok(1.5)]]), []),
    ).toBeNull();
    expect(
      urlPreview("/customers", new Map(), [{ name: "q", preview: null }]),
    ).toBeNull();
  });
});
