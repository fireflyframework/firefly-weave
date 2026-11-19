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
// A call step's output as the compiler infers it (compiler/calls.py
// call_output_schema), pinned by studio/tests/fixtures/callable-corpus.json.
// The scope walker and the call step's descriptor both read it.
/** A JSON Schema object, as the scope walker and the forms hold them. */
type Schema = Record<string, unknown>;

/** The result status a waiting call that continues on failure reports (compiler/calls.py CALL_STATUSES). */
const CALL_STATUSES = ["succeeded", "failed", "cancelled", "timed_out"];

/**
 * A call step's output: null for a call that doesn't wait, the result status
 * around the called workflow's output when a waiting call continues on
 * failure, or that output itself. An unknown output is any value.
 */
export function callOutputSchema(
  mode: unknown,
  onFailure: unknown,
  calleeOutput: Schema | null,
): Schema {
  if (mode === "detach") return { type: "null" };
  const output = calleeOutput ?? {};
  if (onFailure !== "continue") return output;
  return {
    type: "object",
    properties: {
      status: { type: "string", enum: CALL_STATUSES },
      runId: { type: "string", format: "uuid" },
      output,
      error: {
        type: "object",
        properties: {
          code: { type: "string" },
          message: { type: "string" },
        },
        required: ["code", "message"],
        additionalProperties: false,
      },
    },
    required: ["status", "runId"],
    additionalProperties: false,
  };
}
