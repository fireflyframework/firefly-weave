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
  aiConnectionRequest,
  aiConnections,
  aiConnector,
} from "../src/app/integrations/ai-provider-connection";
const draft = {
  name: "azure-models",
  provider: "azure-responses",
  endpoint: "https://approved.openai.azure.com/",
  apiVersion: "2025-04-01-preview",
  handle: "model-key",
};
describe("AI provider connection authority", () => {
  it.each([
    "http://model.invalid",
    "https://user:password@model.invalid",
    "https://model.invalid/?api-key=secret",
    "https://model.invalid/#secret",
  ])(
    "rejects an endpoint carrying authority outside HTTPS origin: %s",
    (endpoint) => {
      expect(() =>
        aiConnectionRequest({ ...draft, endpoint }, "connector"),
      ).toThrow();
    },
  );
  it("requires the explicit Azure API version and operator handle", () => {
    expect(() =>
      aiConnectionRequest({ ...draft, apiVersion: "" }, "connector"),
    ).toThrow("Azure requires");
    expect(() =>
      aiConnectionRequest({ ...draft, handle: "key with spaces" }, "connector"),
    ).toThrow("secret handle");
  });
  it("keeps provider/model separate and derives only the approved origin", () => {
    const body = aiConnectionRequest(draft, "connector");
    expect(body.allowed_destinations).toEqual([
      "https://approved.openai.azure.com",
    ]);
    expect(body.config).not.toHaveProperty("model");
    expect(body.secretRef).toEqual({ apiKey: "model-key" });
    expect(
      aiConnectionRequest(
        { ...draft, provider: "openai-responses" },
        "connector",
      ).config,
    ).not.toHaveProperty("apiVersion");
  });
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
