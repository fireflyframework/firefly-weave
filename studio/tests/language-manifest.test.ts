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
  pendingStepFields,
  readyKinds,
  type LanguageManifest,
} from "../src/app/editor/language-manifest";

describe("language manifest marks", () => {
  it("requires descriptors only for kinds marked ready", () => {
    const manifest: LanguageManifest = {
      version: "weave/language-manifest-v1",
      language_version: "weave/v1alpha1",
      ir_versions: [],
      features: [],
      limits: {},
      operators: [],
      workflow_fields: [],
      step_kinds: [
        {
          kind: "action",
          schema_ref: "workflow.schema.json#/$defs/ActionStep",
          group: "actions",
          label: "Action",
          blocks: [],
          scope_roots: [],
          studio: "ready",
        },
        {
          kind: "forEach",
          feature: "flow.forEach",
          schema_ref: "workflow.schema.json#/$defs/ForEachStep",
          group: "flow",
          label: "Loop over items",
          blocks: [{ name: "body", path: "/body", label: "For each item" }],
          scope_roots: ["/item", "/index", "/loops"],
          studio: "pending",
        },
      ],
    };
    expect(readyKinds(manifest)).toEqual(["action"]);
  });
});

describe("shared step settings", () => {
  const manifest: LanguageManifest = {
    version: "weave/language-manifest-v1",
    language_version: "weave/v1alpha1",
    ir_versions: [],
    features: [],
    limits: {},
    operators: [],
    workflow_fields: [],
    step_kinds: [],
    step_fields: [
      {
        name: "undo",
        feature: "flow.transaction",
        kinds: ["action", "callWorkflow"],
        values: ["reserve", "compensate", "irreversible"],
        paths: {
          reserve: ["/confirm", "/cancel", "/holdSeconds"],
          compensate: ["/compensate"],
          irreversible: ["/irreversible"],
        },
        only_inside: "transaction",
        studio: "pending",
      },
      {
        name: "onError",
        feature: "flow.onError",
        kinds: ["action", "transform"],
        values: ["stop", "continue", "errorOutput"],
        path: "/onError",
        default: "stop",
        block: { name: "errorPath", path: "/errorPath", label: "On error" },
        studio: "pending",
      },
    ],
  };
  it("lists the fields of pending settings for the kinds that hold them", () => {
    expect(pendingStepFields(manifest, "action")).toEqual([
      "confirm",
      "cancel",
      "holdSeconds",
      "compensate",
      "irreversible",
      "onError",
      "errorPath",
    ]);
    expect(pendingStepFields(manifest, "transform")).toEqual([
      "onError",
      "errorPath",
    ]);
    expect(pendingStepFields(manifest, "wait")).toEqual([]);
  });
  it("reads a manifest from a platform without shared settings", () => {
    const { step_fields: _, ...older } = manifest;
    expect(pendingStepFields(older, "action")).toEqual([]);
  });
});
