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
import { ApiError, type StudioApi } from "../src/app/api";
import {
  connectorPage,
  languageTarget,
  catalogScope,
  forgetCatalog,
  loadConnectors,
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

  const fakeApi = (project: string, answer: () => Promise<unknown>) => {
    const calls: string[] = [];
    const api = {
      project,
      page: (path: string) => {
        calls.push(path);
        return answer();
      },
    } as unknown as StudioApi;
    return { api, calls };
  };
  const one = () =>
    Promise.resolve({
      items: [{ adapter: "weave-email", reference: "weave-email@1.0.0" }],
      next_cursor: null,
    });

  it("keeps the installed apps per platform, account and workspace", async () => {
    forgetCatalog();
    const { api, calls } = fakeApi("/p/1", one);
    const acme = catalogScope({ name: "Acme" }, { principal_id: "u-1" }, api);
    const globex = catalogScope(
      { name: "Globex" },
      { principal_id: "u-1" },
      api,
    );
    const other = catalogScope({ name: "Acme" }, { principal_id: "u-2" }, api);
    expect(new Set([acme, globex, other]).size).toBe(3);
    expect(await loadConnectors(api, acme)).toHaveLength(1);
    expect(await loadConnectors(api, acme)).toHaveLength(1);
    expect(calls).toHaveLength(1);
    await loadConnectors(api, globex);
    await loadConnectors(api, other);
    expect(calls).toHaveLength(3);
  });

  it("forgets a platform's apps when it signs out or goes, and keeps the others", async () => {
    forgetCatalog();
    const { api, calls } = fakeApi("/p/1", one);
    const acme = catalogScope({ name: "Acme" }, { principal_id: "u-1" }, api);
    const globex = catalogScope(
      { name: "Globex" },
      { principal_id: "u-1" },
      api,
    );
    await loadConnectors(api, acme);
    await loadConnectors(api, globex);
    forgetCatalog("Acme");
    await loadConnectors(api, acme);
    await loadConnectors(api, globex);
    expect(calls).toHaveLength(3);
  });

  it("asks once for apps the account may not read, and again after other failures", async () => {
    forgetCatalog();
    const forbidden = fakeApi("/p/1", () =>
      Promise.reject(new ApiError(403, { code: "WV-FORBIDDEN" })),
    );
    const scope = catalogScope(
      { name: "Acme" },
      { principal_id: "u-1" },
      forbidden.api,
    );
    expect(await loadConnectors(forbidden.api, scope)).toBeNull();
    expect(await loadConnectors(forbidden.api, scope)).toBeNull();
    expect(forbidden.calls).toHaveLength(1);
    const down = fakeApi("/p/2", () => Promise.reject(new ApiError(503, {})));
    const there = catalogScope(
      { name: "Acme" },
      { principal_id: "u-1" },
      down.api,
    );
    await loadConnectors(down.api, there);
    await loadConnectors(down.api, there);
    expect(down.calls).toHaveLength(2);
  });
});
