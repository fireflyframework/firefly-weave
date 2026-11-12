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
// What the shell and step details read of the run open in the Runs tab.
// Types only: the shell imports this file without loading the Runs tab's
// code, which stays in its own chunk.
import type { CanvasRun } from "../canvas/run-state";
import type { OmissionReason } from "../../operate/run-contracts";

type Json = unknown;

/** One step instance's recorded output, as step details show it. */
export type RecordedOutput =
  | { kind: "value"; value: Json }
  | { kind: "omitted"; reason: OmissionReason }
  | { kind: "none"; why: "not-reached" | "running" | "not-recorded" }
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "too-large" };

/** What a step received, recomputed from the run's data, or why it can't be. */
export type Received =
  | { ok: true; value: Json }
  | { ok: false; reason: string };

/** The data of one run, kept in memory while it is open. */
export interface RunDataPort {
  readonly runId: string;
  readonly environmentId: string;
  /** The run's input. */
  input(): Json;
  /** The recorded output of a step instance (its instance key). */
  output(key: string): RecordedOutput;
  /** What the step received; `key` picks one instance of a step that ran several times. */
  received(stepId: string, key?: string): Received;
  /** The step's instance keys, the failing one first. */
  iterations(stepId: string): readonly string[];
  /** The error code the step failed with in this run; null when it didn't fail. */
  failure(stepId: string): string | null;
  /** The run a call step started; null when none. */
  childRun(key: string): string | null;
  /** "run 3f2a…" */
  label(): string;
}

/** The run open in the Runs tab, as the shell draws it. */
export interface RunViewPort {
  /** The run as the canvas draws it; null until it has loaded. */
  canvas(): CanvasRun | null;
  readonly data: RunDataPort;
  /** Forgets the run and stops following it. */
  close(): void;
}
