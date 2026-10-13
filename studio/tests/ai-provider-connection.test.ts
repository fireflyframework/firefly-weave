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
  aiConnections,
  aiConnector,
  latestByName,
  secretRefs,
} from "../src/app/integrations/ai-provider-connection";
describe("AI connection revisions", () => {
  it("excludes unavailable and unrelated connection revisions", () => {
    const valid = {
      id: "ai",
      connector: aiConnector,
      config: { provider: "azure-chat", endpoint: "https://example.invalid" },
    };
    expect(
      aiConnections([
        valid,
        { ...valid, id: "hidden", unavailable: true },
        { ...valid, id: "http", connector: "weave-http@2.0.0" },
      ]),
    ).toEqual([valid]);
  });
});
describe("AI connection lists", () => {
  const base = {
    connector: aiConnector,
    config: { provider: "openai-chat", endpoint: "http://ollama:11434/v1" },
  };
  it("keeps the latest revision of each name, sorted by name", () => {
    // The platform's order is not alphabetical, so the result shows the sort.
    const rows = [
      { ...base, id: "b1", name: "openai", revision: 1 },
      { ...base, id: "a1", name: "ollama-local", revision: 1 },
      { ...base, id: "a2", name: "ollama-local", revision: 2 },
    ];
    expect(latestByName(rows as never).map((row) => row.id)).toEqual([
      "a2",
      "b1",
    ]);
  });
  it("reads secret handles under either wire spelling", () => {
    expect(
      secretRefs({ secretRef: { apiKey: "no-credential" } } as never),
    ).toEqual({ apiKey: "no-credential" });
    expect(secretRefs({ secret_refs: { apiKey: "k" } } as never)).toEqual({
      apiKey: "k",
    });
    expect(secretRefs({} as never)).toEqual({});
  });
});
