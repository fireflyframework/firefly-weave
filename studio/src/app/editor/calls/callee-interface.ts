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
// What a call step knows about the workflow it calls: its input, its result
// and who may call it, read from the project's catalog lock or from a draft
// on this computer, and the step's own output as the compiler infers it
// (compiler/calls.py call_output_schema, pinned by
// studio/tests/fixtures/callable-corpus.json).
import {
  isJsonObject,
  type Json,
  type JsonObject,
} from "../../forms/core/json";
import { lockNote, type LocalCallee } from "./callable-catalog";
import type { CatalogLockState } from "./catalog-lock";

/** A called workflow's interface: what `KindContext.workflowContract` answers. */
export interface CalleeContract {
  inputSchema: JsonObject | null;
  outputSchema: JsonObject | null;
  /** The version's `spec.callable`, or null when it has none. */
  callable: Json | null;
}
export type Callee =
  | {
      state: "found";
      /** Published in this project, or a draft on this computer. */
      source: "project" | "local";
      contract: CalleeContract;
    }
  /** The project's versions are still being read. */
  | { state: "loading"; contract: null }
  /** The step names no `name@version`. */
  | { state: "unset"; contract: null; message: string }
  /** The project doesn't have this version (removed, or never published). */
  | { state: "missing"; contract: null; message: string }
  /** The project's versions can't be read here, and no draft on this computer has it. */
  | { state: "unavailable"; contract: null; message: string };

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
  calleeOutput: JsonObject | null,
): JsonObject {
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

function contractOf(document: unknown): CalleeContract {
  const spec =
    isJsonObject(document) && isJsonObject(document["spec"])
      ? document["spec"]
      : {};
  const schema = (value: Json | undefined) =>
    isJsonObject(value) ? value : null;
  return {
    inputSchema: schema(spec["inputSchema"]),
    outputSchema: schema(spec["outputSchema"]),
    callable: spec["callable"] ?? null,
  };
}

/**
 * The workflow `uses` (`name@version`) names: the project's published
 * version when the lock has it, otherwise a draft on this computer with that
 * reference, otherwise why Studio can't find it.
 */
export function findCallee(
  uses: string,
  sources: { lock: CatalogLockState; drafts: readonly LocalCallee[] },
): Callee {
  const at = uses.indexOf("@");
  const name = at > 0 ? uses.slice(0, at) : "",
    version = at > 0 ? uses.slice(at + 1) : "";
  if (!name || !version)
    return {
      state: "unset",
      contract: null,
      message: "Choose a workflow to call.",
    };
  const { lock } = sources;
  const published =
    lock.kind === "ready" ? lock.lock.workflows.get(uses) : undefined;
  if (published)
    return {
      state: "found",
      source: "project",
      contract: contractOf(published.document),
    };
  const draft = sources.drafts.find(
    (entry) => entry.name === name && entry.version === version,
  );
  if (draft)
    return {
      state: "found",
      source: "local",
      contract: contractOf(draft.document),
    };
  if (lock.kind === "loading") return { state: "loading", contract: null };
  if (lock.kind === "ready")
    return {
      state: "missing",
      contract: null,
      message: `Studio can't find ${name} ${version} in this project.`,
    };
  return { state: "unavailable", contract: null, message: lockNote(lock)! };
}
