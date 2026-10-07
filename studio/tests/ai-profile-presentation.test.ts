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
  aiProfileSections,
  mergeProfileSection,
  profileSectionData,
} from "../src/app/integrations/ai-profile-presentation";

const profile = {
  type: "object",
  additionalProperties: false,
  properties: {
    provider: { type: "string", enum: ["anthropic", "openai-responses"] },
    model: { type: "string", minLength: 1 },
    options: { $ref: "#/$defs/Options" },
    reasoning: {
      type: "object",
      properties: {
        pattern: { type: "string" },
        maxSteps: { type: "integer", maximum: 32 },
      },
    },
    maxCalls: { type: "integer", maximum: 64 },
  },
  required: ["provider", "model", "options"],
};
const definitions = {
  Options: {
    type: "object",
    properties: {
      max_tokens: { type: "integer", minimum: 1, maximum: 32768 },
      temperature: { type: "number", minimum: 0, maximum: 2 },
      stop_sequences: { type: "array", items: { type: "string" } },
    },
    required: ["max_tokens"],
    additionalProperties: false,
  },
};
const schema = { ...profile, $defs: definitions };

describe("AI profile presentation", () => {
  it("projects basic and advanced fields without changing the canonical schema or constraints", () => {
    const original = structuredClone(schema);
    const sections = aiProfileSections(schema);
    expect(sections.basic.paths).toEqual([
      ["provider"],
      ["model"],
      ["options", "max_tokens"],
    ]);
    expect(sections.advanced.paths).toContainEqual(["options", "temperature"]);
    expect(sections.advanced.paths).toContainEqual(["reasoning", "maxSteps"]);
    expect(
      sections.basic.schema.properties?.options.properties?.max_tokens.maximum,
    ).toBe(32768);
    expect(
      sections.advanced.schema.properties?.options.properties?.temperature
        .maximum,
    ).toBe(2);
    expect(schema).toEqual(original);
  });
  it("merges only owned fields, preserving unknown fields and cleared advanced options across basic edits", () => {
    const sections = aiProfileSections(schema);
    const initial = {
      provider: "anthropic",
      model: "approved",
      options: { max_tokens: 512, temperature: 0.5, future_option: "keep" },
      reasoning: { pattern: "none", maxSteps: 6 },
      future_field: { keep: true },
    };
    const staleBasic = profileSectionData(initial, sections.basic);
    const advanced = profileSectionData(initial, sections.advanced);
    delete (advanced.options as Record<string, unknown>).temperature;
    let result = mergeProfileSection(initial, advanced, sections.advanced);
    staleBasic.model = "another-approved-model";
    result = mergeProfileSection(result, staleBasic, sections.basic);
    expect(result).toEqual({
      provider: "anthropic",
      model: "another-approved-model",
      options: { max_tokens: 512, future_option: "keep" },
      reasoning: { pattern: "none", maxSteps: 6 },
      future_field: { keep: true },
    });
    expect(initial.options.temperature).toBe(0.5);
  });
  it("handles the Lumi configuration wrapper and keeps root settings in advanced", () => {
    const wrapper = {
      type: "object",
      properties: {
        enabled: { type: "boolean" },
        profile: { $ref: "#/$defs/Profile" },
      },
      required: ["profile"],
      $defs: { ...definitions, Profile: profile },
    };
    const sections = aiProfileSections(wrapper);
    expect(sections.basic.paths).toContainEqual([
      "profile",
      "options",
      "max_tokens",
    ]);
    expect(sections.advanced.paths).toContainEqual(["enabled"]);
    const data = {
      enabled: false,
      profile: {
        provider: "anthropic",
        model: "approved",
        options: { max_tokens: 512, temperature: 1 },
      },
    };
    const basic = profileSectionData(data, sections.basic);
    (basic.profile as Record<string, unknown>).model = "updated";
    expect(mergeProfileSection(data, basic, sections.basic)).toEqual({
      ...data,
      profile: { ...data.profile, model: "updated" },
    });
  });
});
