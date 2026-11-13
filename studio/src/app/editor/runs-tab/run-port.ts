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
  /** Changes whenever the run's data does: views read it to redraw. */
  version(): number;
  /** Reads a step's recorded outputs, once per step while the run is open. */
  read(stepId: string): void;
  /** The platform refused a read of the run: the Runs tab forgets what it read. */
  refused(): void;
  /** The run's input. */
  input(): Json;
  /** The recorded output of a step instance (its instance key). */
  output(key: string): RecordedOutput;
  /** What the step received, recomputed with the version the run used. */
  received(stepId: string): Received;
  /** How one step instance went in the run. */
  state(key: string): "failed" | "running" | "finished" | "not-reached";
  /** The step's instance keys, the failing one first. */
  iterations(stepId: string): readonly string[];
  /** The error code the step failed with in this run; null when it didn't fail. */
  failure(stepId: string): string | null;
  /** The run a call step started; null when none. */
  childRun(key: string): string | null;
  /** "run 3f2a…" */
  label(): string;
  /** The version the run used ("1.2.0"); null when Studio couldn't read it. */
  definitionVersion(): string | null;
}

/** The Runs tab and the run open in it, as the shell draws them. */
export interface RunViewPort {
  /** The run as the canvas draws it; null until it has loaded. */
  canvas(): CanvasRun | null;
  /** The chosen run's data; null while none is chosen or it loads. */
  readonly data: RunDataPort | null;
  /** The list has runs to choose from. */
  readonly listed: boolean;
  /** Forgets the run and stops following it. */
  close(): void;
}
