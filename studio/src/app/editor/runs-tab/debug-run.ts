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
// Debug in editor (a failed run) and Copy to editor (any other): the run's
// data becomes the draft's test data, then Execute workflow runs on the
// Studio host with it and stops where the run failed, with the run's error
// on that step. Every read is the person's own (run.read); the host's check
// sees every entry before it is kept; a change of account, environment or
// workflow while it works changes nothing.
import { ApiError } from "../../api";
import type { DialogChoice, DialogOptions } from "../../dialog";
import { describeError } from "../../errors";
import { shortId } from "../../format";
import type { StructuredCanvasAdapter, Workflow } from "../../model";
import {
  listRunSteps,
  type RunViewsApi,
  type StepFact,
} from "../../operate/run-contracts";
import type { Json } from "../ndv/registry";
import type { StepDetailsRequest } from "../ndv/step-details-service";
import type { ExecuteOutcome } from "../state/execution-service";
import type { ExecuteTarget } from "../state/execution";
import { instanceStep } from "../state/instance-keys";
import { entriesOf } from "../state/test-data-ops";
import type { TestDataService } from "../state/test-data-service";
import { runPrints } from "../state/test-data-tools";
import type { ToastAction } from "../../toast";
import {
  leftOutText,
  mergeQuestion,
  mergedTestData,
  testDataFromRun,
} from "./debug-from-run";
import { OVERLAY_FACTS_LIMIT } from "./run-overlay";
import type { RunNotice } from "../ndv/step-details-host";
import type { ChosenRun } from "./runs-tab-state";

/** What Debug in editor needs from Studio's shell. */
export interface DebugHost {
  readonly profile: { environmentId?: string | null } | null;
  /** The account's names, when the platform gave them. */
  readonly workspaceNames?: { environment?: string } | null;
  readonly api: RunViewsApi;
  readonly draftId: string;
  readonly model: StructuredCanvasAdapter;
  readonly stepDataScope: string;
  readonly testData: TestDataService | null;
  readonly dialogs: {
    choose(options: DialogOptions): Promise<DialogChoice | null>;
  };
  runNotice: RunNotice | null;
  can(capability: string): boolean;
  notify(
    text: string,
    action?: ToastAction,
    tone?: "neutral" | "danger",
    detail?: string,
  ): void;
  selectTab(tab: string): Promise<boolean>;
  executeIn(
    target: ExecuteTarget,
    options?: { fresh?: boolean },
  ): Promise<ExecuteOutcome>;
  openStepDetails(
    target: string,
    request?: Partial<StepDetailsRequest>,
  ): Promise<boolean>;
}
export type DebugOutcome =
  | { kind: "done"; stoppedAt: string | null }
  | { kind: "refused"; message: string }
  | { kind: "cancelled" }
  | { kind: "stale" };

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const OUTPUT_PAGE = 200;
export const LOCAL_ONLY =
  "This workflow is kept on this computer only. Save it to the platform to copy a run's data into its test data.";
export const ROLE_CANT = "Your role can't change this workflow's test data.";
export const UNKNOWN_ENVIRONMENT =
  "Studio can't tell which environment this run is from, so it can't keep its data.";

/** Why Debug in editor and Copy to editor can't run here, known before a click; null when they can. */
export function debugRefusal(host: DebugHost): string | null {
  if (!host.profile || host.testData?.place().kind !== "connected")
    return LOCAL_ONLY;
  if (!["run.read", "simulate", "definition.write"].every((c) => host.can(c)))
    return ROLE_CANT;
  if (!UUID.test(host.profile.environmentId ?? "")) return UNKNOWN_ENVIRONMENT;
  return null;
}

export class DebugRun {
  async run(
    host: DebugHost,
    chosen: ChosenRun,
    intent: "debug" | "copy",
    /** Told when the Merge or Replace question opens, and when copying goes on. */
    phase: (phase: "asking" | "copying") => void = () => {},
  ): Promise<DebugOutcome> {
    const refusal = debugRefusal(host);
    if (refusal) return { kind: "refused", message: refusal };
    const record = chosen.record;
    const definition = chosen.data ? chosenDefinition(chosen) : null;
    if (!record || !definition)
      return {
        kind: "refused",
        message:
          "Studio couldn't read the version this run used, so it can't copy its data.",
      };
    const service = host.testData!;
    // Everything below applies only while the account, environment, workflow and run hold.
    const fence = {
      scope: host.stepDataScope,
      environment: host.profile?.environmentId ?? "",
      draftId: host.draftId,
      run: chosen.id,
    };
    const holds = () =>
      host.stepDataScope === fence.scope &&
      (host.profile?.environmentId ?? "") === fence.environment &&
      host.draftId === fence.draftId;
    let facts: StepFact[];
    try {
      facts = await readOutputs(host.api, chosen.id);
    } catch (error) {
      if (!holds()) return { kind: "stale" };
      // Refused: this account can't read the run any more, so the tab forgets
      // it as any refused read of it does.
      if (error instanceof ApiError && error.status === 403) {
        chosen.data?.refused();
        return { kind: "stale" };
      }
      return { kind: "refused", message: describeError(error).message };
    }
    if (!holds()) return { kind: "stale" };
    const now = new Date().toISOString();
    const built = testDataFromRun(
      {
        runId: chosen.id,
        environmentId: fence.environment,
        status: record.status,
        record,
        facts,
        definition,
        failedStep: failedStepOf(chosen),
      },
      host.model.definition,
      runPrints(definition, record.input as Json, host.model),
      now,
    );
    if ("refused" in built) return { kind: "refused", message: built.refused };
    const short = shortId(chosen.id);
    const current = service.data();
    let next = built.data;
    const existing = entriesOf(current).length;
    if (existing) {
      phase("asking");
      const choice = await host.dialogs.choose({
        title: `Use run ${short}'s data as test data?`,
        message: mergeQuestion(
          built,
          existing,
          host.workspaceNames?.environment || "this environment",
        ),
        confirmLabel: "Merge",
        alternateLabel: "Replace",
        cancelLabel: "Cancel",
        // Merge loses nothing: the question starts on it.
        startOnConfirm: true,
      });
      if (!choice) return { kind: "cancelled" };
      phase("copying");
      if (!holds()) return { kind: "stale" };
      if (choice === "confirm")
        next = mergedTestData(service.data(), built, now);
    }
    const kept = await service.keepFromRun(next, built.pointers, holds);
    if (!holds() || (!kept.ok && "stale" in kept)) return { kind: "stale" };
    if (!kept.ok) return { kind: "refused", message: kept.message };
    const left = leftOutText(built.leftOut, kept.refused);
    // Said once Execute has answered, with where it stopped; Undo puts the
    // test data back as it was.
    const say = (stop: string) =>
      host.notify(
        `Test data now holds run ${short}'s data.`,
        {
          label: "Undo",
          run: () => {
            if (!service.restore(kept.previous, kept.version))
              host.notify("The test data changed since, so Undo did nothing.");
          },
        },
        "neutral",
        [stop, left].filter(Boolean).join(" ") || undefined,
      );
    await host.selectTab("Designer");
    if (!holds()) return { kind: "stale" };
    // The run's data alone: an earlier execution's results feed no step.
    const outcome = await host.executeIn({ mode: "workflow" }, { fresh: true });
    if (!holds()) return { kind: "stale" };
    const stoppedAt = outcome.kind === "done" ? stopOf(outcome) : null;
    const failedHere =
      !!stoppedAt && !!built.stopAt && stoppedAt === built.stopAt.key;
    say(
      failedHere
        ? `Execute workflow stopped at ${instanceStep(stoppedAt)}, where the run failed.`
        : "",
    );
    if (!stoppedAt) return { kind: "done", stoppedAt: null };
    // The step Execute stopped at, its Output first; the run's error there
    // when that is where the run failed (Debug in editor).
    if (failedHere && intent === "debug")
      host.runNotice = {
        draftId: fence.draftId,
        runId: chosen.id,
        key: built.stopAt!.key,
        code: built.stopAt!.code,
        missing:
          outcome.kind === "done" &&
          outcome.response.blocked?.reason === "missing_mock",
      };
    await host.openStepDetails(instanceStep(stoppedAt), {
      focus: { kind: "region", region: "output" },
    });
    return { kind: "done", stoppedAt };
  }
}

/** Every step instance with its output (include=output), 200 a page, halving on a page too large. */
async function readOutputs(
  api: RunViewsApi,
  runId: string,
): Promise<StepFact[]> {
  const facts: StepFact[] = [];
  let cursor: string | undefined;
  let limit = OUTPUT_PAGE;
  for (;;) {
    let page;
    try {
      page = await listRunSteps(api, runId, {
        include: "output",
        limit,
        ...(cursor ? { cursor } : {}),
      });
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.status === 429 &&
        error.code === "WV-PAGE-LIMIT" &&
        limit > 1
      ) {
        limit = Math.floor(limit / 2);
        continue;
      }
      throw error;
    }
    facts.push(...page.items);
    // Past what the overlay reads, testDataFromRun refuses the run whole.
    if (!page.next_cursor || facts.length > OVERLAY_FACTS_LIMIT) return facts;
    cursor = page.next_cursor;
  }
}

/** The version the run used, as the Runs tab read it. */
function chosenDefinition(chosen: ChosenRun): Workflow | null {
  return chosen.definition ?? null;
}
/** Where the run failed: the summary's failed step, else its first incident. */
function failedStepOf(
  chosen: ChosenRun,
): { key: string; code: string | null } | null {
  const failed = chosen.summary?.failed_step;
  if (failed)
    return {
      key: failed.instance_key || failed.node_id,
      code: failed.error_code,
    };
  const incident = chosen.record?.incidents[0];
  return incident ? { key: incident.key, code: incident.code } : null;
}
/** The instance the simulation stopped at: blocked, or failed. */
function stopOf(
  outcome: Extract<ExecuteOutcome, { kind: "done" }>,
): string | null {
  const response = outcome.response;
  if (response.blocked)
    return response.blocked.instanceKey || response.blocked.nodeId;
  const failed = response.trace.find((entry) => entry.status === "failed");
  return failed ? failed.instanceKey || failed.nodeId : null;
}
