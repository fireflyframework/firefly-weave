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
import { describe, expect, it } from "vitest";
import {
  connectorPage,
  languageTarget,
} from "../src/app/editor/add-step/catalog-sources";

describe("what the target runs", () => {
  it("reads the features and what Studio can't edit yet from the language manifest", () => {
    expect(
      languageTarget({
        features: ["text.concat", 7],
        step_kinds: [
          { kind: "forEach", studio: "pending" },
          { kind: "transform", studio: "ready" },
        ],
        workflow_fields: [{ name: "callable", studio: "pending" }],
      }),
    ).toEqual({
      features: ["text.concat"],
      pending: new Set(["forEach", "callable"]),
    });
  });

  it("reads nothing from what isn't a manifest", () => {
    expect(languageTarget({ items: [], next_cursor: null })).toBeNull();
    expect(languageTarget("<!doctype html>")).toBeNull();
    expect(languageTarget(null)).toBeNull();
  });
});

describe("the installed apps", () => {
  it("reads a page of connector descriptors", () => {
    expect(
      connectorPage({
        items: [
          {
            adapter: "weave-email",
            reference: "weave-email@1.0.0",
            digest: "x",
          },
          { adapter: 7 },
        ],
        next_cursor: "page-2",
      }),
    ).toEqual({
      items: [{ adapter: "weave-email", reference: "weave-email@1.0.0" }],
      next: "page-2",
    });
    expect(connectorPage({ items: [], next_cursor: null })).toEqual({
      items: [],
      next: null,
    });
    expect(connectorPage({ code: "WV-FORBIDDEN" })).toBeNull();
  });
});
