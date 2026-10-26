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
// Shared simulated answers and fingerprints for the execution tests.
import type { ExecuteResponse } from "../src/app/editor/state/execution";
import type { StepFingerprints } from "../src/app/editor/state/fingerprints";

export const print = (n: number): StepFingerprints => {
  const f =
    `fnv1a64:${String(n).padStart(16, "0")}` as StepFingerprints["execution"];
  return { config: f, contract: f, upstream: f, execution: f };
};
export const COMPILE = { ok: true, sliced: false, diagnostics: [], stubs: [] };
export const RESPONSE: ExecuteResponse = {
  status: "completed",
  compile: COMPILE,
  trace: [
    {
      index: 0,
      nodeId: "check",
      instanceKey: "",
      kind: "action",
      status: "completed",
      input: { id: "c" },
      output: { ok: true },
    },
    {
      index: 1,
      nodeId: "send",
      instanceKey: "send[0]",
      kind: "action",
      status: "completed",
      output: { id: "m-0" },
    },
    {
      index: 2,
      nodeId: "inner",
      instanceKey: "",
      kind: "action",
      status: "completed",
      output: 1,
      frame: "enrich",
    },
    {
      index: 3,
      nodeId: "gone",
      instanceKey: "",
      kind: "transform",
      status: "completed",
      output: 2,
    },
  ],
};
