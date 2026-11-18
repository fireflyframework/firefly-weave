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
// Execute workflow in <environment>: the whole saved draft runs for real, as
// a test run, on a test activation of the environment Studio is connected
// to. It asks first when steps change data there, binds the connections this
// computer bound to the workflow's slots, and opens the run in the Runs tab,
// which follows it live over the draft.
import type { DialogOptions, DialogService } from "../../dialog";
import { describeError } from "../../errors";
import type { RowMenuItem } from "../../row-menu";
import { connectionsPort } from "../connections/connections-port";
import type { StepDetailsHost } from "../ndv/step-details-host";
import { handOverLinkedRun } from "../runs-tab/run-links";
import type {
  DraftTestBindings,
  EnvironmentTestPolicy,
} from "../state/execution";

/** What Execute workflow in an environment needs from the editor. */
export type WorkflowTestHost = Pick<
  StepDetailsHost,
  | "api"
  | "profile"
  | "can"
  | "runAccount"
  | "stepDataScope"
  | "environmentName"
  | "draftId"
  | "draftRevision"
  | "keptLocally"
  | "editingLocked"
  | "model"
  | "workflowSlots"
  | "execution"
  | "flushAutosave"
  | "runCommand"
  | "error"
  | "notify"
  | "selectTab"
  | "viewingRuns"
>;

/** Why Execute workflow in the environment can't run now; null when it can. */
export function workflowTestReason(state: {
  environment: string;
  policy: EnvironmentTestPolicy | "unreadable" | null;
  readOnly: string | null;
  unsaved: boolean;
}): string | null {
  const env = state.environment;
  if (state.readOnly) return state.readOnly;
  if (state.unsaved)
    return `Save this workflow to the platform to execute it in ${env}.`;
  if (state.policy === null)
    return `Studio is checking whether test calls are on in ${env}.`;
  if (state.policy === "unreadable")
    return `Your account can't see whether test calls are on in ${env}.`;
  if (state.policy.test_calls === "disabled")
    return `Test calls are off in ${env}. Choose an environment where they're on to execute the workflow there.`;
  return null;
}

/** "a, b and c". */
const listed = (names: readonly string[]) =>
  names.length < 2
    ? (names[0] ?? "")
    : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;

/** The question asked before a test run whose steps change data in the environment. */
export function sideEffectQuestion(
  steps: readonly string[],
  environment: string,
): DialogOptions {
  return {
    title: "Execute steps that change data?",
    message: steps.length
      ? `${listed(steps)} ${steps.length === 1 ? "changes" : "change"} data in ${environment}. Executing the workflow repeats the change.`
      : `This workflow changes data in ${environment}. Executing it repeats the change.`,
    confirmLabel: "Run it",
    cancelLabel: "Cancel",
    // Focus starts on Cancel: a held or repeated Enter mustn't run a change.
    danger: true,
  };
}

/** The connections this computer bound to the workflow's slots, as the test activation binds them. */
export function workflowTestBindings(
  slots: readonly { name: string }[],
  binding: (slot: string) => { revisionId: string } | null,
): DraftTestBindings {
  const ids: Record<string, string> = {};
  for (const slot of slots) {
    const bound = binding(slot.name);
    if (bound) ids[slot.name] = bound.revisionId;
  }
  return Object.keys(ids).length ? { connection_revision_ids: ids } : {};
}

/**
 * The menu entry: absent without a platform environment or the step tester
 * role, and on the Runs tab; disabled with the reason when it can't run.
 */
export function workflowTestItem(
  host: WorkflowTestHost,
  dialogs: DialogService,
): RowMenuItem | null {
  const port = connectionsPort(host);
  if (!port || host.viewingRuns || !host.can("step.test")) return null;
  const environment = port.environment.name;
  const running = port.runningWorkflowTest();
  const reason = running
    ? `Studio is starting the test run in ${environment}.`
    : workflowTestReason({
        environment,
        policy: port.policyNow(),
        readOnly: host.editingLocked
          ? "This workflow is read only here."
          : null,
        unsaved: !!host.keptLocally && host.draftRevision === undefined,
      });
  return {
    label: `Execute workflow in ${environment}`,
    detail: reason ?? "Runs the whole workflow for real, as a test run",
    disabled: !!reason,
    run: () => void executeWorkflowIn(host, dialogs),
  };
}

/**
 * Runs the saved draft as a test run in the environment and opens the run in
 * the Runs tab; a refusal is said in a toast. Nothing runs when the person
 * cancels the question.
 */
export async function executeWorkflowIn(
  host: WorkflowTestHost,
  dialogs: DialogService,
): Promise<void> {
  const port = connectionsPort(host);
  if (!port) return;
  const environment = port.environment.name;
  const service = port.testActivations(host, ({ steps }) =>
    dialogs.confirm(sideEffectQuestion(steps, environment)),
  );
  const workflow = host.model.definition.metadata.name;
  const bindings = workflowTestBindings(host.workflowSlots, (slot) =>
    port.binding(workflow, slot),
  );
  const draftId = host.draftId ?? "";
  try {
    const outcome = await port.trackWorkflowTest(
      service.execute(host.execution?.workflowInput() ?? {}, bindings),
    );
    if (outcome.ok) {
      handOverLinkedRun(draftId, outcome.value.run.id);
      await host.selectTab?.("Runs");
      return;
    }
    if (outcome.refusal.kind === "cancelled") return;
    host.notify(outcome.refusal.message, undefined, "danger");
  } catch (error) {
    host.notify(
      `The test run didn't start: ${describeError(error).message}`,
      undefined,
      "danger",
    );
  }
}
