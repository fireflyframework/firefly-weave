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
// The language manifest as the platform (`language.read`) and the Studio host
// (`GET /studio/contracts/language`) serve it: the single list of step kinds,
// operators, workflow fields and features. Its fields are snake_case.

/** "ready" once Studio ships the entry's editor; "pending" until then. */
export type StudioMark = "ready" | "pending";
export interface ManifestBlock {
  name: string;
  /** Points at a branch: its steps are at path + "/steps". */
  path: string;
  label: string;
}
export interface ManifestStepKind {
  kind: string;
  feature?: string | null;
  schema_ref: string;
  group: string;
  label: string;
  blocks: ManifestBlock[];
  scope_roots: string[];
  studio: StudioMark;
}
export interface ManifestOperator {
  name: string;
  feature?: string | null;
  arity: { min: number; max?: number };
  operand_types?: string[][];
  repeat_last?: boolean;
  item_types?: string[];
  result?: { type: string };
  label: string;
  studio: StudioMark;
}
export interface ManifestWorkflowField {
  name: string;
  feature?: string | null;
  studio: StudioMark;
}
export interface LanguageManifest {
  version: "weave/language-manifest-v1";
  language_version: string;
  ir_versions: string[];
  features: string[];
  limits: Record<string, number>;
  operators: ManifestOperator[];
  workflow_fields: ManifestWorkflowField[];
  step_kinds: ManifestStepKind[];
}

/** Step kinds Studio must have a descriptor for; pending kinds may lack one. */
export function readyKinds(manifest: LanguageManifest): string[] {
  return manifest.step_kinds
    .filter((entry) => entry.studio === "ready")
    .map((entry) => entry.kind);
}
