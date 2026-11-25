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
// Why a workflow isn't published because of its transactions: the errors that
// stop it, each with its fix, as the question before publishing lists them.
// Other errors stay in Diagnostics.
import { signal, type WritableSignal } from "@angular/core";
import {
  diagnosticRows,
  type DiagnosticInput,
} from "../../designer/diagnostics-list";
import type { DiagnosticLocation } from "../../designer/diagnostic-location";
import type { TransactionFix } from "./fixes";

/** A transaction's error, where it is and its fix. */
export interface TransactionProblem {
  /** The step it is on. */
  step: string;
  text: string;
  fix: TransactionFix | null;
  location: DiagnosticLocation;
}
export interface PublishSummary {
  name: string;
  version: string;
  problems: readonly TransactionProblem[];
}
interface CheckResult {
  diagnostics?: readonly DiagnosticInput[];
}

const OURS = /^WV-(?:COMP-TXN_|STUDIO-(?:TRANSACTION|UNDO)$)/;

/** The errors of a check about transactions, in the order the check lists them. */
export function transactionProblems(
  result: CheckResult | null,
  definition: unknown,
): TransactionProblem[] {
  return diagnosticRows(result?.diagnostics ?? [], definition).problems.flatMap(
    ({ view, location, fix }) =>
      view.severity === "error" && OURS.test(view.code) && location?.exact
        ? [{ step: location.stepId, text: view.text, fix, location }]
        : [],
  );
}

/** "This transaction isn't safe to publish: 2 problems". */
export const problemsText = (problems: readonly TransactionProblem[]): string =>
  `This transaction isn't safe to publish: ${problems.length} ${problems.length === 1 ? "problem" : "problems"}`;

const open = new WeakMap<object, WritableSignal<PublishSummary | null>>();
/** The summary an editor window shows, or null. */
export function publishSummary(
  host: object,
): WritableSignal<PublishSummary | null> {
  let found = open.get(host);
  if (!found) open.set(host, (found = signal<PublishSummary | null>(null)));
  return found;
}

/** Shows what a check found on the workflow's transactions; false when it found nothing. */
export function showTransactionProblems(
  host: {
    readonly model: {
      readonly definition: { metadata: { name: string; version: string } };
    };
    readonly diagnosticsDefinition?: unknown;
  },
  result: CheckResult | null,
): boolean {
  const problems = transactionProblems(result, host.diagnosticsDefinition);
  if (!problems.length) return false;
  const { name, version } = host.model.definition.metadata;
  publishSummary(host).set({ name, version, problems });
  return true;
}
