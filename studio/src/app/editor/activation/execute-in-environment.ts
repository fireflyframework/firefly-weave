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
import type { Json } from "../../forms/core/json";
import type { TriggerIntent } from "../state/canvas-sidecar";
import type { RowMenuItem } from "../../row-menu";
import { connectionsPort } from "../connections/connections-port";
import {
  calledReferenceSites,
  callPins,
  proposePins,
  type CalleeActivation,
  type CalleeVersion,
} from "./callee-pins";
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
      ? `${listed(steps)} ${steps.length === 1 ? "changes" : "change"} data in ${environment}. Executing the workflow repeats ${steps.length === 1 ? "the change" : "the changes"}.`
      : `This workflow changes data in ${environment}. Executing it repeats the change.`,
    confirmLabel: "Run it",
    cancelLabel: "Cancel",
    // Focus starts on Cancel: a held or repeated Enter mustn't run a change.
    // Run it stays amber, as in the step's own Execute in <environment> question.
    startOnCancel: true,
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
 * The activation each workflow the draft calls runs on, as the activation
 * dialog pins them (the latest activation of the called version), or which
 * called versions aren't active in the environment.
 */
export function calleePins(
  definition: unknown,
  versions: readonly CalleeVersion[],
  activations: readonly CalleeActivation[],
  environment: string,
): { pins: Record<string, string> } | { refusal: string } {
  const sites = calledReferenceSites(definition);
  if (!sites.length) return { pins: {} };
  const rows = proposePins(sites, [...versions], [...activations]);
  const inactive = rows
    .filter((row) => row.kind !== "proposed")
    .map((row) => row.reference.replace("@", " "));
  if (!inactive.length) return { pins: callPins(rows) };
  return {
    refusal:
      inactive.length === 1
        ? `This workflow calls ${inactive[0]}, which isn't active in ${environment}. Activate it first.`
        : `This workflow calls ${listed(inactive)}, which aren't active in ${environment}. Activate them first.`,
  };
}

const text = (value: unknown) => (typeof value === "string" ? value : "");
const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
/** Pages read of each list (published versions, activations), at most. */
const MAX_PAGES = 10;

/**
 * The pins of the workflows the draft calls, read from the project's
 * published versions and the environment's activations; none are read for a
 * draft that calls nothing.
 */
async function readCalleePins(
  host: WorkflowTestHost,
  environment: string,
): Promise<{ pins: Record<string, string> } | { refusal: string }> {
  const definition = host.model.definition;
  if (!calledReferenceSites(definition).length) return { pins: {} };
  const pages = async (collection: string, inEnvironment: boolean) => {
    const items: Record<string, unknown>[] = [];
    let cursor: string | undefined;
    for (let page = 0; page < MAX_PAGES; page++) {
      const answer = await host.api.page(collection, inEnvironment, cursor);
      items.push(...answer.items);
      if (!answer.next_cursor) break;
      cursor = answer.next_cursor;
    }
    return items;
  };
  const [versions, activations] = await Promise.all([
    pages("workflows", false),
    pages("activations", true),
  ]);
  return calleePins(
    definition,
    versions.map((item) => ({
      id: text(item["id"]),
      name: text(item["name"]),
      version: text(item["version"]),
    })),
    activations.map((item) => ({
      id: text(item["id"]),
      name: text(item["name"]),
      revision: Number(item["revision"]) || 0,
      retired: item["retired"] === true,
      versionId: text(record(item["request"])["version_id"]),
    })),
    environment,
  );
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
  // Read when the menu draws its rows or a row is picked, never while the
  // editor bar draws: the bar mustn't ask for the test policy on every render.
  const reason = () =>
    port.runningWorkflowTest()
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
    get detail() {
      return reason() ?? "Runs the whole workflow for real, as a test run";
    },
    get disabled() {
      return !!reason();
    },
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
  const opened = host.model.opened;
  try {
    // Each workflow the draft calls runs on its activation here, as an activation pins it.
    const called = await readCalleePins(host, environment);
    if (host.model.opened !== opened || (host.draftId ?? "") !== draftId)
      return;
    if ("refusal" in called) {
      host.notify(called.refusal, undefined, "danger");
      return;
    }
    const pinned: DraftTestBindings = Object.keys(called.pins).length
      ? { ...bindings, workflow_activation_ids: called.pins }
      : bindings;
    const outcome = await port.trackWorkflowTest(
      service.execute(host.execution?.workflowInput() ?? {}, pinned),
    );
    if (outcome.ok) {
      port.ownRuns.add(outcome.value.run.id);
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

/** What listening for a test event needs from the editor. */
export type ListenHost = WorkflowTestHost & Pick<StepDetailsHost, "testData">;

/** Starts listening for the webhook's test event on a test URL of the environment. */
export function listenFor(
  host: ListenHost,
  trigger: TriggerIntent,
  dialogs: DialogService,
  payloadSample?: Json,
): void {
  const port = connectionsPort(host);
  if (!port) return;
  const environment = port.environment.name;
  const listener = port.testListener(host, ({ steps }) =>
    dialogs.confirm(sideEffectQuestion(steps, environment)),
  );
  const workflow = host.model.definition.metadata.name;
  const bindings = workflowTestBindings(host.workflowSlots, (slot) =>
    port.binding(workflow, slot),
  );
  const opened = host.model.opened;
  void (async () => {
    let called: Awaited<ReturnType<typeof readCalleePins>>;
    try {
      called = await readCalleePins(host, environment);
    } catch (error) {
      host.notify(
        `Couldn't check the called workflows: ${describeError(error).message}`,
        undefined,
        "danger",
      );
      return;
    }
    if (host.model.opened !== opened) return;
    if ("refusal" in called) {
      host.notify(called.refusal, undefined, "danger");
      return;
    }
    await listener.start(
      trigger,
      Object.keys(called.pins).length
        ? { ...bindings, workflow_activation_ids: called.pins }
        : bindings,
      payloadSample,
    ).done;
  })();
}

/** Stops listening, which retires the test URL. */
export function stopListening(host: WorkflowTestHost): void {
  void connectionsPort(host)?.madeListener()?.stop();
}
