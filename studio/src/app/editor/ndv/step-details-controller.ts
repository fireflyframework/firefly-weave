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
// The editing side of step details for one workflow: the forms of a step
// (or the trigger, End and workflow settings), what they read, and every
// write as one undo step. A burst of edits to one field within 600 ms is one
// undo step; a change that hides fields clears them in the same step and
// says so with an Undo toast. Registered parameter components get the
// registry's NdvContext from here.
import { connectionsPort } from "../connections/connections-port";
import { signal, untracked } from "@angular/core";
import { safeSample } from "./panes/sample";
import {
  canonicalJson,
  getAt,
  isJsonObject,
  parsePointer,
} from "../../forms/core/json";
import { referenceScope, type ScopeOptions } from "../../forms/core/scope";
import { locateDiagnostic } from "../../designer/diagnostic-location";
import {
  stepIdentities,
  type StructuredCanvasAdapter,
  type Step,
  type Workflow,
} from "../../model";
import { applyEdits } from "./edits";
import { findStep } from "./step-walk";
import {
  endForm,
  triggerForm,
  workflowSettingsForm,
} from "./kinds/forms/workflow-forms";
import {
  ownedActionsOf,
  usesOwning,
  withOwnedAction,
} from "./owned/owned-store";
import { moveFileStep, reservedActionNames } from "./owned/insert-owned";
import {
  forkEdited,
  publishedVersionsOf,
  versionMoveText,
  type VersionMove,
} from "./owned/owned-versions";
import {
  CANVAS_TOO_BIG,
  canvasBytes,
  stepNotesOf,
  triggerIntents,
  withStepNote,
  withTriggerIntents,
  type CanvasSidecar,
  type StepNote,
  type TriggerIntent,
} from "../state/canvas-sidecar";
import {
  triggerForm as triggerSettingsForm,
  triggerProblems,
} from "../triggers/trigger-forms";
import { isTriggerChoice, triggerChoiceOf } from "../triggers/trigger-target";
import {
  CANVAS_MAX_BYTES,
  type AiTurnsEntry,
  type DataSource,
  type ErrorEntry,
  type HumanEntry,
  type Provenance,
  type SignalEntry,
} from "../state/test-data";
import {
  aiTaskReplay,
  aiTaskTurn,
  holdsAnswer,
  resultSchemaOf,
  textSample,
  type TurnProvenance,
} from "./kinds/ai-task-model";
import { TestDataService } from "../state/test-data-service";
import { checkInputsOf } from "../state/test-data-inputs";
import {
  failureOf,
  withFailure,
  withInput,
  withPin,
  withoutInput,
  withoutPin,
  withScript,
} from "../state/test-data-ops";
import {
  fingerprints,
  staleReason,
  type StepFingerprints,
} from "../state/fingerprints";
import {
  contractOf,
  keepPinned as keepPin,
  ownedContract,
  pinsView,
  stampedPin,
  type PinView,
} from "../state/pins-stale";
import { sampleFromSchema } from "./panes/sample";
import type { StoreBackend } from "../state/browser-store";
import { nameOfUses, type OwnedRecipe } from "./owned/owned-actions";
import {
  formEnv,
  stepSubject,
  workflowSubject,
  WORKFLOW_STAND_IN,
} from "./params/form-env";
import {
  formState,
  hiddenChanges,
  resetParam,
  type FormEnv,
} from "./params/form-model";
import { NO_PREVIEW, type PreviewHook } from "./params/preview";
import {
  applyChange,
  type FormChange,
  type FormSubject,
} from "./params/value-io";
import {
  ndvRegistry,
  type Edit,
  type Expression,
  type FormSpec,
  type Json,
  type KindContext,
  type NdvContext,
  type ParamSpec,
  type Path,
  type SampleContext,
  type StaleReason,
  type StepKindDescriptor,
} from "./registry";
import type { StepDetailsHost } from "./step-details-host";
import type { BlockedReason } from "../state/execution";
import type {
  HandledResult,
  LastResult,
  RealResult,
} from "../state/last-results";
import { shortId } from "../../format";
import { instanceStep } from "../state/instance-keys";
import type {
  RecordedOutput,
  Received,
  RunDataPort,
} from "../runs-tab/run-port";
import type { HostDiagnostic } from "../../integrations/http-action-client";
import {
  PendingEntries,
  type PendingEntry,
  type PendingOwner,
} from "./pending-entries";
import type { PreviewData } from "../formula/preview";
import { LoopScopes, enclosingLoops } from "../formula/loops";
import { isRecordedRun, type RecordedRun } from "../canvas/run-state";
import { Iterations, itemPicks, resultItems } from "../state/iterations";
import { SIGN_IN_TO_LIST, type TableCatalog } from "./table-catalog";
import { parse as parseYaml, stringify as stringifyYaml } from "yaml";
import type { ActionDraftEntry } from "../state/action-drafts";
import { HttpActionClient } from "../../integrations/http-action-client";
import type { StepTables } from "./registry";
import { nextDraftVersion, type TableDocument } from "../state/table-drafts";
import {
  CONNECT_TO_PUBLISH,
  ROLE_CANT_PUBLISH,
  UNKNOWN_OUTCOME,
  publishFailure,
  publishTable,
} from "../decision-grid/publish-table";
import type { TableSpec } from "../decision-grid/rules-model";
import {
  callSources,
  workflowRows,
  type CallSources,
} from "../calls/call-sources";
import {
  endedOnError,
  onErrorFields,
  onErrorKinds,
  removedPathToast,
  type OnErrorContext,
  type OnErrorWrite,
} from "./settings/on-error";

/**
 * A pin's step as toasts name it: the step, and inside a loop's body the
 * item as people count it ("send for item 3", "reserve for item 3 › 1"),
 * never the instance key.
 */
export function pinSubject(key: string): string {
  const item = pinItem(key);
  return item ? `${instanceStep(key)} for item ${item}` : instanceStep(key);
}
/** Whose answer an AI task's toast names: "summarize", or "summarize, item 2" inside a loop. */
function answerSubject(key: string): string {
  const item = pinItem(key);
  return item ? `${instanceStep(key)}, item ${item}` : instanceStep(key);
}
/** The item of a pin inside a loop's body, one-based ("3", "3 › 1"); null for a step's pin. */
function pinItem(key: string): string | null {
  const items = [...key.slice(instanceStep(key).length).matchAll(/\[(\d+)\]/g)];
  return items.length
    ? items
        .map((match) => (Number(match[1]) + 1).toLocaleString("en-US"))
        .join(" › ")
    : null;
}

export type ControllerHost = Pick<
  StepDetailsHost,
  | "model"
  | "perform"
  | "notify"
  | "undo"
  | "undoIfUnchanged"
  | "catalogContracts"
  | "decisionContracts"
  | "actionVersions"
  | "cacheContract"
  | "label"
  | "editingLocked"
  | "runData"
  | "viewingRuns"
  | "runView"
  | "profile"
  | "stepDataScope"
  | "localDrafts"
  | "openWorkflow"
  | "removeTrigger"
  | "api"
  | "diagnostics"
  | "diagnosticsDefinition"
  | "cacheDecisionContract"
  | "can"
  | "refreshView"
  | "testData"
  | "testDataPrints"
  | "openTestData"
  | "pinFromElsewhere"
  | "execution"
  | "runAccount"
  | "environmentName"
  | "runNotice"
  | "openedItem"
  | "draftId"
>;
/** A diagnostic of one step, located in it. */
/** Whether a schema marks any value secret, anywhere in it. */
const marksSecret = (schema: unknown): boolean =>
  schema !== null &&
  schema !== undefined &&
  /"(x-secret|writeOnly)"\s*:\s*true/.test(JSON.stringify(schema));

/** The last execution's answer about a step, in its Output pane. */
export interface ExecuteNotice {
  /** The step it is about. */
  target: string;
  message: string;
  /** Why the run stopped there; null for a refusal or a failure. */
  reason: BlockedReason | null;
  /** "Edit test input" is the way out. */
  input?: boolean;
  /** A way out of its own: Open in Operate, Execute previous steps, Fix. */
  action?: { label: string; run: () => void };
}
export interface StepDiagnostic {
  code: string;
  severity: string;
  message: string;
  path: string;
  at: Path;
  suggestedEdit?: unknown;
}
/**
 * A trigger choice's invalid settings, as its fields show them. An empty
 * required field says so itself; these are the values the platform would refuse.
 */
function choiceDiagnostics(choice: TriggerIntent): StepDiagnostic[] {
  return Object.entries(triggerProblems(choice))
    .filter(([field]) => {
      const value = choice[field];
      return value !== undefined && value !== "";
    })
    .map(([field, message]) => ({
      code: "WV-STUDIO-TRIGGER",
      severity: "error",
      message,
      path: `/${field}`,
      at: [field],
    }));
}
export interface CommitOutcome {
  ok: boolean;
  /** Labels of fields the change hid and cleared. */
  removed: string[];
}

const PSEUDO = new Set(["$trigger", "$end", "$workflow"]);
/** The input form, End, the workflow's settings, or a trigger choice: no step of the workflow. */
const pseudo = (target: string): boolean =>
  PSEUDO.has(target) || isTriggerChoice(target);
const pointer = (path: Path) =>
  path
    .map((s) => "/" + String(s).replace(/~/g, "~0").replace(/\//g, "~1"))
    .join("");
const beneath = (path: string, root: string): boolean =>
  path === root || path.startsWith(`${root}/`);
const BURST_MS = 600;

/** Generate sample on output fields Studio can't sample. */
export const CANT_SAMPLE =
  "Can't make a sample for this output; set it by hand.";

export class StepDetailsController {
  // --- test data: the test input and pinned output --------------------------
  /** The shell's test data, or this controller's own, in memory (tests, a shell without one). */
  private ownTestData: TestDataService | null = null;
  get testData(): TestDataService {
    if (this.host.testData) return this.host.testData;
    this.ownTestData ??= this.memoryTestData();
    return this.ownTestData;
  }
  private memoryTestData(): TestDataService {
    const nothing: StoreBackend = {
      keys: async () => [],
      get: async () => null,
      set: async () => false,
      remove: async () => false,
    };
    // Without the shell nothing is kept and no host is asked: the test input
    // reaches here with its secret fields removed already (safeSample), and
    // nothing else is accepted without a check.
    return new TestDataService({
      api: {
        request: async <T>() =>
          ({ entries: [{ path: "/input", ok: true }] }) as T,
      },
      backend: nothing,
      now: () => new Date(),
      inputs: () => {
        let contracts: ReadonlyMap<string, Record<string, unknown>>;
        try {
          contracts = this.host.catalogContracts;
        } catch {
          contracts = new Map();
        }
        const inputs = checkInputsOf(this.host.model.definition, contracts);
        return {
          ...inputs,
          schemaOf: (pointer: string) =>
            inputs.schemaOf(pointer) ?? (pointer === "/input" ? {} : undefined),
        };
      },
    });
  }
  /** The workflow, connection and account test data belongs to. */
  private eventScope: {
    model: StructuredCanvasAdapter;
    opened: number;
    profile: string;
    account: string | undefined;
    api: ControllerHost["api"];
  } | null = null;
  /** An opening of the test data that belonged to the previous workflow or account: shown as nothing. */
  private hidden: number | null = null;
  private syncTestEvent(): void {
    const { model, profile, api } = this.host;
    if (
      this.eventScope?.model === model &&
      this.eventScope.opened === model.opened &&
      this.eventScope.profile === canonicalJson(profile) &&
      this.eventScope.account === this.host.stepDataScope &&
      this.eventScope.api === api
    )
      return;
    const first = this.eventScope === null;
    this.eventScope = {
      model,
      opened: model.opened,
      profile: canonicalJson(profile),
      account: this.host.stepDataScope,
      api,
    };
    if (first) {
      this.host.openTestData?.();
      return;
    }
    // Another workflow, platform or account: what is open belongs to the
    // previous one until the right test data opens.
    if (!this.host.testData) {
      this.ownTestData = this.memoryTestData();
      return;
    }
    const generation = this.host.testData.generation();
    if (this.host.openTestData?.() !== false)
      untracked(() => (this.hidden = generation));
  }
  /** True while the test data on show belongs to an earlier workflow or account. */
  private stale(): boolean {
    if (this.hidden === null) return false;
    if (this.testData.generation() !== this.hidden) this.hidden = null;
    return this.hidden !== null;
  }
  testEvent(): Json | undefined {
    // A run's own input, in run mode: never the draft's test data.
    const run = this.run();
    if (run) {
      run.version();
      return run.input() as Json;
    }
    if (this.viewingRuns()) return undefined;
    this.syncTestEvent();
    const service = this.testData;
    service.version();
    if (this.stale()) return undefined;
    return safeSample(
      this.host.model.definition.spec["inputSchema"],
      service.data().input?.value,
    );
  }
  /** Deferred pane work belongs to the workflow and connection that created it. */
  testEventOwner(): () => boolean {
    this.syncTestEvent();
    const scope = this.eventScope;
    const service = this.testData;
    const generation = service.generation();
    return () => {
      this.syncTestEvent();
      return (
        this.eventScope === scope &&
        this.testData === service &&
        service.generation() === generation &&
        !this.stale()
      );
    };
  }
  /** Keeps the trigger's Test event as the test input; null when kept, else why not. */
  private eventWrite: Promise<unknown> = Promise.resolve();
  private eventTicket = 0;
  /** Typing writes in order, and only the latest value waiting is checked and kept. */
  setTestEvent(
    value: Json | undefined,
    source: "manual" | "schema" = "manual",
  ): Promise<string | null> {
    const ticket = ++this.eventTicket;
    const run = this.eventWrite.then(() =>
      ticket === this.eventTicket ? this.writeTestEvent(value, source) : null,
    );
    this.eventWrite = run.catch(() => null);
    return run;
  }
  private async writeTestEvent(
    value: Json | undefined,
    source: "manual" | "schema",
  ): Promise<string | null> {
    this.syncTestEvent();
    if (this.stale()) return "This workflow's test data is still opening.";
    const service = this.testData;
    const now = new Date().toISOString();
    // A secret field never reaches the check, nor anything it keeps.
    const safe = safeSample(
      this.host.model.definition.spec["inputSchema"],
      value,
    );
    const data = service.data();
    const outcome =
      safe === undefined
        ? await service.change(withoutInput(data, now), [])
        : await service.change(withInput(data, safe, source, now), ["/input"]);
    return outcome.ok ? null : outcome.message;
  }
  /** Every step's fingerprints now: the shell's (memoized), or computed here. */
  prints(): ReadonlyMap<string, StepFingerprints> {
    const shared = this.host.testDataPrints?.();
    if (shared) return shared;
    const model = this.host.model;
    return fingerprints({
      workflow: model.definition,
      testInput: this.testData.data().input?.value,
      contractOf: (step) =>
        contractOf(step, {
          owned: (uses) => ownedContract(model.canvas, uses),
          profiles:
            (model.definition.spec["llmProfiles"] as
              | Record<string, unknown>
              | undefined) ?? {},
        }),
    });
  }
  /** A step's own pin (its step ID) and whether it is out of date; null without one. */
  pinOfTarget(target: string): PinView | null {
    this.syncTestEvent();
    const service = this.testData;
    service.version();
    const entry = service.data().pins?.[target];
    if (this.stale() || !entry) return null;
    // One item's pin (send[2]): judged against its step's fingerprints.
    const stepId = instanceStep(target);
    if (stepId !== target)
      return {
        key: target,
        entry,
        stale: staleReason(entry, this.prints().get(stepId)),
      };
    return pinsView(service.data(), this.prints()).get(target) ?? null;
  }
  /** A step's pinned output as previews may read it: secret fields left out. */
  pinnedOutput(key: string): Json | undefined {
    const pin = this.pinOfTarget(key);
    if (!pin) return undefined;
    const stepId = instanceStep(key);
    const schema = this.descriptor(stepId)?.outputSchema(
      this.step(stepId)!,
      this.kindContext(),
    );
    return safeSample(schema ?? {}, pin.entry.output);
  }
  // --------------------------------------------------- run mode
  /** The run open in the Runs tab; step details then show its data, read-only. */
  run(): RunDataPort | null {
    return this.host.runData ?? null;
  }
  /**
   * The Runs tab: step details show runs that happened, never the draft's
   * test data or last results, and change nothing.
   */
  viewingRuns(): boolean {
    return !!this.host.viewingRuns || !!this.run();
  }
  /** Each step's chosen iteration in run mode (its instance key). */
  readonly iteration = signal<Readonly<Record<string, string>>>({});
  /**
   * The instance key run mode shows for a step: inside loops, the item the
   * Runs tab's chips show; else the chosen iteration, else the first
   * (failed first).
   */
  runKey(target: string): string {
    const run = this.run();
    if (!run || instanceStep(target) !== target) return target;
    run.version();
    const chip = this.chipKey(target);
    if (chip !== null) return chip;
    const keys = run.iterations(target);
    const chosen = this.iteration()[target];
    return chosen && keys.includes(chosen) ? chosen : (keys[0] ?? target);
  }
  /**
   * Run mode's Item select: inside loops it moves the Runs tab's chips, so
   * the canvas and step details show one item; elsewhere it is the step's.
   */
  chooseRunIteration(target: string, key: string): void {
    const pick = this.recordedRun()?.pick;
    const picks = itemPicks(this.host.model.definition, key);
    if (pick && picks.length) {
      for (const { loopId, index } of picks) pick(loopId, index);
      return;
    }
    this.iteration.update((all) => ({ ...all, [target]: key }));
  }
  /**
   * Run mode's Item options for a step inside loops: every item of its
   * innermost loop, in the items the chips show around it, reached or not;
   * null outside loops or without the run's loops.
   */
  runItems(target: string): { count: number; keys: string[] } | null {
    const loops = this.recordedRun()?.loops;
    const chip = this.chipKey(target);
    if (!loops || chip === null) return null;
    const within = [...chip.matchAll(/\[(\d+)\]/g)]
      .map((match) => Number(match[1]))
      .slice(0, -1);
    const [innermost] = enclosingLoops(this.host.model.definition, target);
    const loop = loops.find(
      (each) => each.id === innermost && each.within.join() === within.join(),
    );
    if (!loop) return null;
    const prefix = target + within.map((index) => `[${index}]`).join("");
    return {
      count: loop.count,
      keys: Array.from({ length: loop.count }, (_, i) => `${prefix}[${i}]`),
    };
  }
  /** The run the Runs tab draws, with the items its chips picked; null elsewhere. */
  private recordedRun(): RecordedRun | null {
    const canvas = this.host.runView?.()?.canvas() ?? null;
    return isRecordedRun(canvas) ? canvas : null;
  }
  /** The step in the items the Runs tab's chips show (`send[2]`); null outside loops. */
  private chipKey(target: string): string | null {
    const picks = this.recordedRun()?.picks;
    if (!picks) return null;
    const loops = enclosingLoops(this.host.model.definition, target).reverse();
    const indexes = loops.map((loopId) => picks[loopId]);
    if (!loops.length || indexes.some((index) => index === undefined))
      return null;
    return target + indexes.map((index) => `[${index}]`).join("");
  }
  /** What a step returned in the run, read from the platform the first time it's shown. */
  runOutput(target: string): RecordedOutput {
    const run = this.run();
    if (!run) return { kind: "none", why: "not-recorded" };
    run.version();
    const stepId = instanceStep(target);
    const key = this.runKey(target);
    const output = run.output(key);
    if (output.kind === "loading") run.read(stepId);
    return output;
  }
  private debuggedItem: string | null = null;
  /**
   * Debug in editor stopped at one item of a loop (send[3]): step details
   * open on that item, once for each stop; a pick made after stays.
   */
  pickRunItem(target: string): void {
    const notice = this.host.runNotice;
    if (
      !notice ||
      notice.draftId !== this.host.draftId ||
      notice.key === this.debuggedItem ||
      instanceStep(notice.key) !== target
    )
      return;
    this.debuggedItem = notice.key;
    for (const { loopId, index } of itemPicks(
      this.host.model.definition,
      notice.key,
    ))
      this.iterations.pick(loopId, index);
  }
  private openedItem: object | null = null;
  /**
   * A run log row opened this step on one item of a loop: step details show
   * that item, once for each row chosen; a pick made after stays.
   */
  pickOpenedItem(
    opened: { draftId: string; key: string } | null | undefined,
    target: string,
  ): void {
    if (
      !opened ||
      opened === this.openedItem ||
      opened.draftId !== this.host.draftId ||
      instanceStep(opened.key) !== target
    )
      return;
    this.openedItem = opened;
    for (const { loopId, index } of itemPicks(
      this.host.model.definition,
      opened.key,
    ))
      this.iterations.pick(loopId, index);
  }
  /** Where a pin of this step goes: the item picked inside a loop's body (send[2]), else the step. */
  pinKey(target: string): string {
    return (
      (!this.viewingRuns() && this.iterations.instanceKey(target)) || target
    );
  }
  /**
   * A step's data for the item picked in the loops around it: its own run
   * (`check[1]`, or a pin for that item) first, else the step's (a pin for
   * every item). Outside loops, and on the Runs tab, the step's.
   */
  pickedStepData(id: string): ReturnType<StepDetailsController["stepData"]> {
    const key = this.viewingRuns() ? null : this.iterations.instanceKey(id);
    return (key ? this.stepData(key) : undefined) ?? this.stepData(id);
  }
  /** Try again after a failed read of a step's outputs. */
  retryRunOutput(target: string): void {
    this.run()?.read(instanceStep(target));
  }
  /** What the step received, recomputed from the run's data. */
  runReceived(target: string): Received {
    const run = this.run();
    if (!run) return { ok: false, reason: "" };
    run.version();
    // Inside a loop: the input of the iteration shown, recomputed from the run.
    return run.received(instanceStep(target), this.runKey(target));
  }

  // --------------------------------------------------- executed data
  /** A step's (or an iteration's) last result, from the last execution on this computer, or null. */
  lastResult(key: string): LastResult | null {
    return this.host.execution?.results()?.steps[key] ?? null;
  }
  /**
   * The failure the last execution's On error handled for this step (inside
   * a loop, for the item picked), and when that execution ran; null when
   * the step didn't fail and go on there.
   */
  handledResult(id: string): { failure: HandledResult; at: string } | null {
    if (this.viewingRuns()) return null;
    const results = this.host.execution?.results();
    const key = this.iterations.instanceKey(id) ?? id;
    const failure = this.handledHere(key)?.handled;
    return results && failure ? { failure, at: results.at } : null;
  }
  /**
   * The last result at `key` when the last execution failed the step there
   * and its On error went on: what later steps read (null, or the error
   * path's result), over a pin the failure stood in for. Once that failure
   * is removed, the pin stands for the step again; null then, and when the
   * step didn't fail and go on.
   */
  private handledHere(key: string): LastResult | null {
    const result = this.lastResult(key);
    if (!result?.handled) return null;
    const pinned = this.pinOfTarget(key) ?? this.pinOfTarget(instanceStep(key));
    return pinned && !this.failureOf(key) ? null : result;
  }
  /**
   * What a step's output reads now, the same for the Output pane, the Input
   * pane and formula previews: its pin, else its last result (still used when
   * out of date), else nothing; a result whose failure was handled while
   * its failure is still set comes before the pin. Secret fields are left out.
   */
  stepData(key: string):
    | {
        value: Json;
        source: "pinned" | "simulated" | "run" | "test-call";
        stale: StaleReason | null;
      }
    | undefined {
    // In run mode a step's data is what it returned in that run, nothing else.
    if (this.viewingRuns()) {
      if (!this.run()) return undefined;
      const output = this.runOutput(key);
      return output.kind === "value"
        ? { value: output.value as Json, source: "run", stale: null }
        : undefined;
    }
    const stepId = instanceStep(key);
    const pin = this.handledHere(key)
      ? null
      : (this.pinOfTarget(key) ?? this.pinOfTarget(stepId));
    if (pin)
      return {
        value: this.pinnedOutput(pin.key) ?? {},
        source: "pinned",
        stale: pin.stale,
      };
    const result = this.lastResult(key);
    if (!result || result.output === undefined) return this.replayedTurn(key);
    const step = this.step(stepId);
    const now = this.prints().get(stepId)?.execution;
    const stale = now === result.execution ? null : ("upstream" as const);
    // A real run's output is as the platform classified and kept it; its
    // output schema's secret fields are still left out here.
    if (result.real) {
      const schema = step
        ? this.descriptor(stepId)?.outputSchema(step, this.kindContext())
        : null;
      const value = safeSample(schema ?? {}, result.output);
      return value === undefined
        ? undefined
        : { value, source: "test-call", stale };
    }
    const schema = step
      ? this.descriptor(stepId)?.outputSchema(step, this.kindContext())
      : null;
    // The host refused every secret value before it simulated, so a result
    // too large to copy field by field is shown as it is unless its output
    // fields mark something secret.
    const value =
      safeSample(schema ?? {}, result.output) ??
      (marksSecret(schema) ? undefined : result.output);
    if (value === undefined) return undefined;
    return { value, source: "simulated", stale };
  }
  /** An AI task not executed yet: what Execute step returns for its saved turn. */
  private replayedTurn(key: string) {
    const step = this.step(instanceStep(key));
    const entry = this.scriptOf(key);
    if (!step || !entry || this.answerSection(key) !== "aiTurns") return;
    const replayed = aiTaskReplay(this.host.model.definition, step, entry);
    const schema = this.descriptor(step.id)?.outputSchema(
      step,
      this.kindContext(),
    );
    const value =
      replayed === undefined ? undefined : safeSample(schema ?? {}, replayed);
    return value === undefined
      ? undefined
      : { value, source: "simulated" as const, stale: null };
  }
  /**
   * An AI task's answer kept as the turn Execute step replays, under `key`
   * (the step, or the item picked in a loop). An answer it replaces can come
   * back with Undo, as a replaced pin can.
   */
  private async keepTurn(
    key: string,
    value: Json,
    provenance: TurnProvenance,
  ): Promise<{ ok: false; message: string } | { ok: true; replaced: boolean }> {
    const locked = this.readOnlyReason(key);
    if (locked) return { ok: false, message: locked };
    const step = this.step(instanceStep(key));
    if (!step)
      return { ok: false, message: "This step's output can't be pinned." };
    const made = aiTaskTurn(
      this.host.model.definition,
      step,
      value,
      provenance,
    );
    if ("problem" in made) return { ok: false, message: made.problem };
    const previous = this.testData.data();
    const replaced = Object.hasOwn(previous.aiTurns ?? {}, key);
    const kept = await this.setScript(key, made.entry as unknown as Json);
    if (!kept.ok) return kept;
    if (replaced)
      this.offerUndo(
        `Replaced the kept answer for ${answerSubject(key)}.`,
        previous,
      );
    return { ok: true, replaced };
  }
  /**
   * Pin output on a real result: kept as test data that came from a run, with
   * the run and its environment, so the platform reads it back only for
   * people who may read that run. Null when pinned, else why not. An AI
   * task's answer becomes the turn Execute step replays instead.
   */
  async pinReal(target: string): Promise<string | null> {
    const real = this.realResult(target);
    const data = this.pickedStepData(target);
    if (!real || data?.source !== "test-call")
      return "There's no output from a real run to pin.";
    if (this.answerSection(target) === "aiTurns") {
      const answer = isJsonObject(data.value)
        ? data.value["result"]
        : undefined;
      if (answer === undefined)
        return "There's no output from a real run to pin.";
      const key = this.pinKey(target);
      const kept = await this.keepTurn(key, answer, {
        source: "test-call",
        runId: real.runId,
        runEnvironmentId: real.environmentId,
      });
      if (!kept.ok) return kept.message;
      // A replaced answer's toast offers Undo instead.
      if (!kept.replaced)
        this.host.notify(
          `Kept this answer as test data for ${answerSubject(key)}. Execute step replays it.`,
        );
      return null;
    }
    return this.pin(this.pinKey(target), data.value, "test-call", {
      runId: real.runId,
      runEnvironmentId: real.environmentId,
    });
  }
  /** Whether the answer an AI task shows is the one its saved turn holds, which Execute step replays. */
  keptAnswer(target: string): boolean {
    if (this.answerSection(target) !== "aiTurns") return false;
    return holdsAnswer(
      this.scriptOf(this.pinKey(target)),
      this.pickedStepData(target)?.value ?? null,
      this.realResult(target)?.runId ?? null,
    );
  }
  /** Removes the saved turn that holds the answer shown, with an Undo toast; true once removed. */
  async forgetAnswer(target: string): Promise<boolean> {
    if (this.readOnlyReason(target) || !this.keptAnswer(target)) return false;
    const key = this.pinKey(target);
    const held = Object.hasOwn(this.testData.data().aiTurns ?? {}, key)
      ? key
      : instanceStep(key);
    return (await this.setScript(held, null)).ok;
  }
  /** Where a step's real result came from, when its last result ran for real in an environment. */
  realResult(key: string): RealResult | null {
    if (this.viewingRuns()) return null;
    return this.lastResult(key)?.real ?? null;
  }
  /** What the last execution asked of this step, or why it couldn't run (shown in its Output). */
  readonly executeNotice = signal<ExecuteNotice | null>(null);
  /** Step details' Execute, for the panes' own buttons (set by step details). */
  executeHook: ((mode: "step" | "before") => void) | null = null;
  requestExecute(mode: "step" | "before"): void {
    this.executeHook?.(mode);
  }
  /** What runs now: Execute's target, or null. */
  running() {
    return this.host.execution?.running() ?? null;
  }
  /** Whether Execute is available here at all (the host loaded it). */
  canExecute(): boolean {
    return !!this.host.execution && !this.readOnlyReason();
  }

  // --------------------------------------------------- test answers
  /** Where a step's test answer lives: signals, humans, AI turns, or nowhere. */
  private answerSection(key: string): "signals" | "humans" | "aiTurns" | null {
    const kind = this.step(instanceStep(key))?.kind as string | undefined;
    return kind === "signal"
      ? "signals"
      : kind === "humanTask"
        ? "humans"
        : this.descriptor(instanceStep(key))?.script === "ai-turns"
          ? "aiTurns"
          : null;
  }
  /** A signal's or a person's test answer: the iteration's first, then the step's; null without one. */
  scriptOf(key: string): Json | null {
    const section = this.answerSection(key);
    if (!section) return null;
    this.testData.version();
    const answers = (this.testData.data()[section] ?? {}) as unknown as Record<
      string,
      Json
    >;
    return answers[key] ?? answers[instanceStep(key)] ?? null;
  }
  /**
   * Keeps (or, with null, removes, with Undo) the step's test answer, checked
   * as all test data is: a value marked secret is refused and nothing is kept.
   */
  async setScript(
    key: string,
    value: Json | null,
  ): Promise<{ ok: true } | { ok: false; message: string }> {
    const section = this.answerSection(key);
    if (!section)
      return {
        ok: false,
        message: "Only signals and people take a test answer.",
      };
    const service = this.testData;
    const previous = service.data();
    // A time-out or an expiry is no value: it has no source. An AI turn
    // keeps where it came from (a sample, a real run), else it's typed in.
    const entry =
      value === null
        ? null
        : ((typeof value === "object" && value !== null && "outcome" in value
            ? value
            : section === "aiTurns"
              ? { source: "manual", ...(value as object) }
              : { ...(value as object), source: "manual" }) as unknown as
            | SignalEntry
            | HumanEntry
            | AiTurnsEntry);
    const pointer = `/${section}/${key.replace(/~/g, "~0").replace(/\//g, "~1")}`;
    const outcome = await service.change(
      withScript(previous, section, key, entry, new Date().toISOString()),
      entry === null ? [] : [pointer],
    );
    if (!outcome.ok) return { ok: false, message: outcome.message };
    if (entry === null)
      this.offerUndo(
        section === "aiTurns"
          ? `Removed the kept answer for ${answerSubject(key)}.`
          : `Removed the test answer of ${key}.`,
        outcome.previous,
      );
    return { ok: true };
  }

  /** The failure simulated for one item (`key`), else for its step; null when none is. */
  failureOf(key: string): ErrorEntry | null {
    this.testData.version();
    return failureOf(this.testData.data(), key)?.entry ?? null;
  }
  /**
   * Keeps the failure simulated at `key` (an item's or a step's), set by
   * hand and checked as all test data is; null removes it, with Undo.
   */
  async setFailure(
    key: string,
    failure: Omit<ErrorEntry, "source"> | null,
  ): Promise<{ ok: true } | { ok: false; message: string }> {
    const service = this.testData;
    const entry: ErrorEntry | null = failure && {
      ...failure,
      source: "manual",
    };
    const pointer = `/errors/${key.replace(/~/g, "~0").replace(/\//g, "~1")}`;
    const outcome = await service.change(
      withFailure(service.data(), key, entry, new Date().toISOString()),
      entry === null ? [] : [pointer],
    );
    if (!outcome.ok) return { ok: false, message: outcome.message };
    if (entry === null)
      this.offerUndo(
        `Removed the simulated failure of ${key}.`,
        outcome.previous,
      );
    return { ok: true };
  }

  /** Whether this step's output can be pinned (actions, AI tasks, Call a workflow that waits…). */
  pinnable(target: string): boolean {
    if (this.viewingRuns()) return false;
    const step = this.step(target);
    const descriptor = this.descriptor(target);
    if (!step || !descriptor) return false;
    return typeof descriptor.pinnable === "function"
      ? descriptor.pinnable(step)
      : descriptor.pinnable;
  }
  private pinPointer = (target: string) =>
    `/pins/${target.replace(/~/g, "~0").replace(/\//g, "~1")}`;
  /** Pins this output (stamped with the step's fingerprints of now); null when kept, else why not. */
  async pin(
    target: string,
    value: Json,
    source: DataSource,
    provenance: Provenance = {},
  ): Promise<string | null> {
    // Inside a loop, the target may be one item (send[2]): the step decides.
    const stepId = instanceStep(target);
    const locked = this.readOnlyReason(stepId);
    if (locked) return locked;
    if (!this.pinnable(stepId)) return "This step's output can't be pinned.";
    if (this.stale()) return "This workflow's test data is still opening.";
    const now = this.prints().get(stepId);
    if (!now) return "This step's output can't be pinned.";
    const service = this.testData;
    const at = new Date().toISOString();
    const replacing = !!service.data().pins?.[target];
    const outcome = await service.change(
      withPin(
        service.data(),
        target,
        stampedPin(value, source, now, at, provenance),
        at,
      ),
      [this.pinPointer(target)],
    );
    if (!outcome.ok) return outcome.message;
    if (replacing)
      this.offerUndo(
        `Replaced the pinned output of ${pinSubject(target)}.`,
        outcome.previous,
      );
    return null;
  }
  /**
   * An item's pin, for every item: one change pins it on the step as it is
   * (its output, source and run) and removes the item's own, so one toast's
   * Undo puts both pins back. Null when done, else why not.
   */
  async pinForEveryItem(item: string): Promise<string | null> {
    const stepId = instanceStep(item);
    if (stepId === item) return null;
    const locked = this.readOnlyReason(stepId);
    if (locked) return locked;
    if (this.stale()) return "This workflow's test data is still opening.";
    const service = this.testData;
    const entry = service.data().pins?.[item];
    if (!entry || !this.pinnable(stepId))
      return "This step's output can't be pinned.";
    const at = new Date().toISOString();
    const outcome = await service.change(
      withoutPin(
        withPin(service.data(), stepId, { ...entry, pinnedAt: at }, at),
        item,
        at,
      ),
      [this.pinPointer(stepId)],
    );
    if (!outcome.ok) return outcome.message;
    this.offerUndo(
      `Pinned ${stepId} for every item. Item ${pinItem(item)}'s own pin was removed.`,
      outcome.previous,
    );
    return null;
  }
  /** Unpins the step's output, with an Undo toast. */
  /** Unpins with an Undo toast; resolves true once the pin is gone. */
  unpin(target: string): Promise<boolean> {
    if (this.readOnlyReason(target)) return Promise.resolve(false);
    const service = this.testData;
    if (!service.data().pins?.[target]) return Promise.resolve(false);
    const previous = service.data();
    const at = new Date().toISOString();
    return service
      .change(withoutPin(previous, target, at), [])
      .then((outcome) => {
        if (outcome.ok)
          this.offerUndo(`Unpinned ${pinSubject(target)}.`, outcome.previous);
        return outcome.ok;
      });
  }
  /** This step's pin came from another workflow and isn't kept here yet. */
  pinFromElsewhere(target: string): boolean {
    return !!this.host.pinFromElsewhere?.(target);
  }
  /** Keep pinned: the same output, current again (today's fingerprints). */
  keepPinned(target: string): void {
    // An item's pin (send[2]) is kept against its step's fingerprints.
    const stepId = instanceStep(target);
    if (this.readOnlyReason(stepId)) return;
    const service = this.testData;
    const entry = service.data().pins?.[target];
    const now = this.prints().get(stepId);
    if (!entry || !now) return;
    const at = new Date().toISOString();
    void service
      .change(withPin(service.data(), target, keepPin(entry, now), at), [])
      .then((outcome) => {
        if (!outcome.ok) return;
        // Inside a loop, the item as people count it.
        this.host.notify(
          stepId === target
            ? `Kept pinned output for ${target}.`
            : `Kept the pinned output of ${pinSubject(target)}.`,
        );
      });
  }
  /**
   * Pins a sample made from the step's output fields. An AI task keeps a
   * sample answer made from its result fields as the turn Execute step replays.
   */
  async generateOutput(target: string): Promise<string | null> {
    const locked = this.readOnlyReason(target);
    if (locked) return locked;
    const step = this.step(target);
    const answer = !!step && this.answerSection(target) === "aiTurns";
    const schema = !step
      ? null
      : answer
        ? resultSchemaOf(this.host.model.definition, step)
        : this.descriptor(target)?.outputSchema(step, this.kindContext());
    if (answer && !schema) return "Choose a model first.";
    if (!schema) return "Studio doesn't know this step's output fields yet.";
    let sample: Json;
    try {
      // An AI task's text answer reads as one (its first example, else a sentence).
      sample =
        (answer ? textSample(this.host.model.definition, step) : null) ??
        (sampleFromSchema(schema) as Json);
    } catch {
      // anyOf, allOf, an unknown format or bounds no value meets.
      return CANT_SAMPLE;
    }
    if (!answer) return this.pin(this.pinKey(target), sample, "schema");
    const kept = await this.keepTurn(this.pinKey(target), sample, {
      source: "simulated",
    });
    return kept.ok ? null : kept.message;
  }
  /** Test data is outside the workflow's undo: a toast offers to put the previous document back. */
  private offerUndo(
    text: string,
    previous: ReturnType<TestDataService["data"]>,
  ) {
    const service = this.testData;
    const version = service.version();
    this.host.notify(text, {
      label: "Undo",
      run: () => {
        if (!service.restore(previous, version))
          this.host.notify("The test data changed since, so Undo did nothing.");
      },
    });
  }
  /** What formula previews read: the test input and pinned output. */
  previewData(): PreviewData {
    return {
      // The same precedence as the Input pane: the test input, else the
      // sample the last Execute went through (a loop's items come from it).
      input: () =>
        this.testEvent() ?? this.host.execution?.sampleUsed() ?? undefined,
      // The same precedence as the Output pane: a pin, else the last result;
      // inside a loop, for the item picked.
      output: (id) => this.pickedStepData(id)?.value,
      stamp: () => [
        this.run()?.version() ?? null,
        this.host.model.revision,
        this.testData.version(),
        this.testData.generation(),
        this.host.execution?.results() ?? null,
        this.host.execution?.sampleUsed() ?? null,
      ],
    };
  }

  readonly features = signal<readonly string[]>([]);
  /** The kinds the target's language manifest lets choose On error; null when it lists none. */
  readonly onErrorKinds = signal<readonly string[] | null>(null);
  /** What On error rules read: the kind context and the manifest's On error kinds. */
  onErrorContext(): OnErrorContext {
    return { ...this.kindContext(), onErrorKinds: this.onErrorKinds() };
  }
  /** What fields in loop bodies can read, from the Studio host. */
  readonly loopScopes = new LoopScopes(
    (body) => this.host.api.request("/studio/local/scope", "POST", body),
    () => this.host.refreshView(),
  );
  private picked: { draft: unknown; iterations: Iterations } | null = null;
  /** The item picked in each loop of the open draft, for the steps inside its body. */
  get iterations(): Iterations {
    const { model } = this.host;
    const draft = model.opened;
    if (this.picked?.draft !== draft)
      this.picked = {
        draft,
        iterations: new Iterations({
          definition: () => model.definition,
          revision: () => model.revision,
          items: async (loopId) => {
            const items = this.step(loopId)?.["items"];
            if (!items || typeof items !== "object") return null;
            const ctx = this.ndvContext(loopId);
            const result = this.previews.preview(
              items as Expression,
              ["items"],
              ctx,
            );
            return result?.ok ? result.value : null;
          },
          progress: () => this.host.execution?.last()?.loops ?? null,
        }),
      };
    return this.picked.iterations;
  }
  /**
   * The item (one-based) each result of a loop's output came from, and the
   * items Skip empty results left out; null when Studio can't tell.
   */
  loopResults(loopId: string): { items: number[]; left: number[] } | null {
    const step = this.step(loopId);
    const results = this.pickedStepData(loopId)?.value;
    if (step?.kind !== "forEach" || !Array.isArray(results)) return null;
    return resultItems(step["collect"], results, this.itemResults(step));
  }
  /**
   * What each item of a loop gave, when its result per item reads the item,
   * its position or a step inside by a plain reference; null otherwise.
   */
  private itemResults(step: Step): unknown[] | null {
    const body = step["body"];
    const output = isJsonObject(body) ? body["output"] : undefined;
    const ref =
      isJsonObject(output) && Object.keys(output).length === 1
        ? output["ref"]
        : null;
    const path = typeof ref === "string" ? parsePointer(ref) : null;
    if (!path) return null;
    const items = this.step(step.id)?.["items"];
    const listed =
      items && typeof items === "object"
        ? this.previews.preview(
            items as Expression,
            ["items"],
            this.ndvContext(step.id),
          )
        : null;
    const list =
      listed?.ok && Array.isArray(listed.value) ? listed.value : null;
    const counted = this.iterations.counts(step.id);
    const count = counted.known ? counted.count : list?.length;
    if (count === undefined) return null;
    const around = this.iterations
      .picks(step.id)
      .map(({ index }) => `[${index}]`)
      .join("");
    const gave: unknown[] = [];
    for (let index = 0; index < count; index++) {
      let value: unknown;
      if (path[0] === "index" && path.length === 1) value = index;
      else if (path[0] === "item" && list)
        value = getAt(list[index], path.slice(1));
      else if (path[0] === "steps" && path[2] === "output") {
        const ran = this.stepData(`${path[1]}${around}[${index}]`);
        if (!ran) return null;
        value = getAt(ran.value, path.slice(3));
      } else return null;
      gave.push(value === undefined ? null : value);
    }
    return gave;
  }
  /**
   * The picked item of each loop around a step, from the loop's Items
   * evaluated against the current data (an inner loop's with the outer
   * loop's item); null outside loops or while an item isn't in the data.
   */
  loopData(
    stepId: string,
  ): Pick<SampleContext, "item" | "index" | "loops"> | null {
    const picks = this.iterations.picks(stepId);
    if (!picks.length) return null;
    const loops: NonNullable<SampleContext["loops"]> = {};
    for (const { loopId, index } of picks) {
      const items = this.step(loopId)?.["items"];
      if (!items || typeof items !== "object") return null;
      const listed = this.previews.preview(
        items as Expression,
        ["items"],
        this.ndvContext(loopId),
      );
      if (!listed?.ok || !Array.isArray(listed.value)) return null;
      if (index >= listed.value.length) return null;
      loops[loopId] = { item: listed.value[index] as Json, index };
    }
    const innermost = loops[picks[picks.length - 1].loopId];
    return { item: innermost.item, index: innermost.index, loops };
  }
  private addedBy = new Map<string, Set<string>>();
  private touchedBy = new Map<string, Set<string>>();
  private burst: { key: string; revision: number; at: number } | null = null;
  private session: { model: StructuredCanvasAdapter; opened: number } | null =
    null;
  private featureLoad: {
    profile: ControllerHost["profile"];
    api: ControllerHost["api"];
    promise: Promise<void>;
  } | null = null;

  constructor(
    private readonly host: ControllerHost,
    private readonly now: () => number = () => Date.now(),
    private readonly previews: PreviewHook = NO_PREVIEW,
  ) {}

  /** The target's language features: the platform's when connected, the Studio host's otherwise. */
  loadFeatures(): Promise<void> {
    const { profile, api } = this.host;
    if (this.featureLoad?.profile === profile && this.featureLoad.api === api)
      return this.featureLoad.promise;
    this.features.set([]);
    this.onErrorKinds.set(null);
    const load = { profile, api, promise: Promise.resolve() };
    this.featureLoad = load;
    const current = () =>
      this.featureLoad === load &&
      this.host.profile === profile &&
      this.host.api === api;
    load.promise = (async () => {
      try {
        const path = profile
          ? `${api.project}/language`
          : "/studio/contracts/language";
        const manifest = await api.request<{ features?: string[] }>(path);
        if (current()) {
          this.features.set(manifest.features ?? []);
          this.onErrorKinds.set(onErrorKinds(manifest));
        }
      } catch {
        if (current()) {
          this.features.set([]);
          this.onErrorKinds.set(null);
          this.featureLoad = null;
        }
      }
    })();
    return load.promise;
  }

  private syncSession(): void {
    const model = this.host.model;
    if (this.session?.model === model && this.session.opened === model.opened)
      return;
    this.session = { model, opened: model.opened };
    this.addedBy.clear();
    this.touchedBy.clear();
    this.revealedBy.clear();
    this.marks++;
    this.burst = null;
  }

  kindContext(workflow: Workflow = this.host.model.definition): KindContext {
    const owned = ownedActionsOf(this.host.model.canvas);
    return {
      workflow,
      features: this.features(),
      actionContract: (uses) => this.actionContract(uses),
      tableContract: (uses) => this.tableContract(uses),
      workflowContract: (uses) => this.callSources().contract(uses),
      callNote: (uses) =>
        this.viewingRuns() ? null : this.callSources().note(uses),
      viewingRun: this.viewingRuns(),
      ownedAction: (uses) =>
        (owned[uses] as unknown as Json | undefined) ?? null,
      actionDraft: (uses) =>
        (this.actionDraft(uses)?.document as unknown as Json | undefined) ??
        null,
    };
  }
  step(target: string): Step | null {
    if (pseudo(target)) return null;
    return (
      this.host.model.nodes().find((node) => node.step.id === target)?.step ??
      null
    );
  }
  descriptor(target: string): StepKindDescriptor | null {
    const step = this.step(target);
    return step ? (ndvRegistry.kind(step.kind) ?? null) : null;
  }
  /** An action's document: published (as cached), or its draft on this computer. */
  actionContract(uses: string): Json | null {
    return (
      (this.host.catalogContracts.get(uses) as Json | undefined) ??
      (this.actionDraft(uses)?.document as unknown as Json | undefined) ??
      null
    );
  }
  /** The draft of a published action's next version this `uses` names, kept on this computer. */
  actionDraft(uses: string): ActionDraftEntry | null {
    const at = uses.lastIndexOf("@");
    const draft = this.tables?.keptActions()?.draft(uses.slice(0, at));
    return draft && `${draft.name}@${draft.version}` === uses ? draft : null;
  }
  /** The recipe of the owned action this step calls, else a draft's spec, else the published action's spec (read-only). */
  private actionOf(step: Step): Json | null {
    const uses = String(step["uses"] ?? "");
    const owned = ownedActionsOf(this.host.model.canvas)[uses];
    if (owned) return owned as unknown as Json;
    const draft = this.actionDraft(uses);
    if (draft) return draft.document.spec as Json;
    const document = this.host.catalogContracts.get(uses);
    return (document?.["spec"] as Json | undefined) ?? null;
  }
  /** The trigger choice a target names, or null. */
  triggerChoice(target: string): TriggerIntent | null {
    return triggerChoiceOf(this.host.model.canvas, target);
  }
  subject(target: string): FormSubject {
    const choice = this.triggerChoice(target);
    if (choice)
      return {
        // A trigger choice's settings sit where a step's fields do.
        step: choice as unknown as Step,
        workflow: this.host.model.definition,
        action: null,
        roots: choice.kind === "provider" ? [["mapping"]] : [],
      };
    const step = this.step(target);
    const descriptor = this.descriptor(target);
    if (!step || !descriptor)
      return workflowSubject(this.host.model.definition);
    return stepSubject(
      descriptor,
      step,
      this.kindContext(),
      this.actionOf(step),
    );
  }
  /** Why nothing can change now, in words for the step, trigger or settings at `target`. */
  readOnlyReason(target?: string): string | null {
    const run = this.run();
    if (run)
      return `You're looking at ${run.label()}. Go back to editing to change this step.`;
    const shown = this.host.runView?.();
    // A chosen run still loading reads as the run it will show.
    if (this.host.viewingRuns && shown?.loading && shown.chosen)
      return `You're looking at run ${shortId(shown.chosen)}. Go back to editing to change this step.`;
    if (this.host.viewingRuns)
      return shown?.listed
        ? "Choose a run to see what this step did in it. Go back to editing to change this step."
        : "Steps are read-only on the Runs tab. Go back to editing to change this step.";
    if (this.host.model.readonly)
      return target === "$workflow"
        ? "Fix the source to edit workflow settings."
        : "Fix the source to edit this step.";
    if (this.host.editingLocked)
      return "A simulation is running. Stop it to edit.";
    return null;
  }
  env(target: string): FormEnv {
    return formEnv(
      this.subject(target),
      this.step(target) ?? WORKFLOW_STAND_IN,
      this.kindContext(),
      this.readOnlyReason(target),
      this.added(target),
    );
  }
  parameters(target: string): FormSpec {
    if (target === "$trigger") return triggerForm();
    const choice = this.triggerChoice(target);
    if (choice) return triggerSettingsForm(choice.kind);
    if (target === "$end") return endForm();
    if (target === "$workflow") return workflowSettingsForm();
    const step = this.step(target);
    return (
      (step && this.descriptor(target)?.form?.(step, this.kindContext())) || {
        fields: [],
      }
    );
  }
  settings(target: string): FormSpec {
    const step = this.step(target);
    const own = (step &&
      this.descriptor(target)?.settings?.(step, this.kindContext())) || {
      fields: [],
    };
    // On error, and Result on error with it, come after the kind's own settings.
    const onError = step ? onErrorFields(step, this.onErrorContext()) : [];
    return onError.length
      ? { ...own, fields: [...own.fields, ...onError] }
      : own;
  }

  private set(map: Map<string, Set<string>>, target: string): Set<string> {
    let found = map.get(target);
    if (!found) map.set(target, (found = new Set()));
    return found;
  }
  added(target: string): ReadonlySet<string> {
    this.syncSession();
    return this.addedBy.get(target) ?? new Set();
  }
  addOption(target: string, id: string): void {
    this.syncSession();
    this.set(this.addedBy, target).add(id);
  }
  touch(target: string, id: string): void {
    this.syncSession();
    const touched = this.set(this.touchedBy, target);
    if (touched.has(id)) return;
    touched.add(id);
    this.marks++;
  }
  touched(target: string): ReadonlySet<string> {
    this.syncSession();
    return this.touchedBy.get(target) ?? new Set();
  }
  private revealedBy = new Set<string>();
  /** Every message of a step shows from now on (Execute step was pressed). */
  reveal(target: string): void {
    this.syncSession();
    if (this.revealedBy.has(target)) return;
    this.revealedBy.add(target);
    this.marks++;
  }
  revealed(target: string): boolean {
    this.syncSession();
    return this.revealedBy.has(target);
  }
  /** Problems the host's builder found in the actions this workflow owns (set by the mount). */
  ownedProblems: () => ReadonlyMap<string, HostDiagnostic[]> = () => new Map();
  /** The registered parameter component open in place of a step's form (the decision table grid). */
  readonly parameterComponent = signal<{
    target: string;
    kind: string;
    subNode?: string;
  } | null>(null);
  /** Keeps a changed action draft; a refusal is said, and the step details read it again. */
  private keepActionDraft(draft: ActionDraftEntry, spec: Json) {
    const tables = this.tables;
    if (!tables) return;
    const account = tables.account();
    void tables.actions().then(async (actions) => {
      if (tables.account() !== account) return;
      try {
        await actions.save(draft, {
          ...draft.document,
          spec: spec as ActionDraftEntry["document"]["spec"],
        });
      } catch (error) {
        this.host.notify(
          error instanceof Error ? error.message : String(error),
          undefined,
          "danger",
        );
      }
      this.host.refreshView();
    });
  }
  /**
   * Change in a new version: the published action's next free patch version
   * starts on this computer and the step moves to it in one undo step.
   */
  async startActionDraft(target: string): Promise<string | null> {
    const step = this.step(target);
    const tables = this.tables;
    if (!step || !tables || this.readOnlyReason(target)) return null;
    const uses = String(step["uses"] ?? "");
    const document = this.host.catalogContracts.get(uses);
    if (!document) return null;
    const model = this.host.model;
    const opened = model.opened;
    const name = uses.slice(0, uses.lastIndexOf("@"));
    const listed = publishedVersionsOf(this.host.actionVersions, name);
    const actions = await tables.actions();
    if (this.host.model !== model || model.opened !== opened) return null;
    if (actions.draft(name)) {
      this.host.notify(
        `${name} ${actions.draft(name)!.version} is already a draft on this computer.`,
      );
      return null;
    }
    let entry: ActionDraftEntry;
    try {
      entry = await actions.startDraft(
        document as unknown as ActionDraftEntry["document"],
        listed,
      );
    } catch (error) {
      this.host.notify(
        error instanceof Error ? error.message : String(error),
        undefined,
        "danger",
      );
      return null;
    }
    if (this.host.model !== model || model.opened !== opened) return null;
    const next = `${entry.name}@${entry.version}`;
    this.commit(
      target,
      [{ scope: "step", path: ["uses"], value: next }],
      `ndv:Start ${entry.name} ${entry.version}`,
    );
    return next;
  }
  /** Publish action: an action draft goes to the platform; the step keeps its `uses`, now published. */
  async publishActionDraft(target: string): Promise<string | null> {
    const step = this.step(target);
    const tables = this.tables;
    const host = this.host;
    if (!step || !tables) return null;
    if (!host.profile) return "Connect to a platform to publish this action.";
    if (!host.can("definition.publish"))
      return "Your role can't publish actions in this project.";
    const uses = String(step["uses"] ?? "");
    const draft = this.actionDraft(uses);
    if (!draft) return `${uses} isn't a draft on this computer.`;
    const key = this.publishKeys.get(uses) ?? crypto.randomUUID();
    this.publishKeys.set(uses, key);
    try {
      await new HttpActionClient(host.api).publishAction(
        stringifyYaml(draft.document),
        key,
      );
    } catch (error) {
      const failure = publishFailure(error);
      if (failure.kind === "unknown")
        return "Studio couldn't tell whether the action was published. Publish action again: it's safe to repeat.";
      this.publishKeys.delete(uses);
      if (failure.kind === "role")
        return "Your role can't publish actions in this project.";
      if (failure.kind === "taken")
        return `${uses} was published by someone else. Change it in a new version again.`;
      return failure.message;
    }
    this.publishKeys.delete(uses);
    const actions = await tables.actions();
    await actions.remove(draft.name);
    host.cacheContract(
      uses,
      draft.document as unknown as Record<string, unknown>,
    );
    host.notify(`Published ${draft.name} ${draft.version}.`);
    host.refreshView();
    return null;
  }
  /**
   * Edit action as YAML: the host checks the document first; the draft keeps
   * it only when it loads as the same action. A sentence when it doesn't.
   */
  async saveActionDraft(target: string, yaml: string): Promise<string | null> {
    const step = this.step(target);
    const tables = this.tables;
    const draft = step ? this.actionDraft(String(step["uses"] ?? "")) : null;
    if (!draft || !tables)
      return "This step's action isn't a draft on this computer.";
    let checked: {
      validationOk?: boolean;
      diagnostics?: { message?: string; severity?: string }[];
    };
    try {
      checked = await this.host.api.request("/studio/local/validate", "POST", {
        source: yaml,
        format: "yaml",
      });
    } catch (error) {
      return error instanceof Error ? error.message : String(error);
    }
    if (checked.validationOk === false) {
      const first = (checked.diagnostics ?? []).find(
        (diagnostic) => diagnostic.severity === "error",
      );
      return first?.message ?? "Weave can't load this action.";
    }
    let document: ActionDraftEntry["document"];
    try {
      document = parseYaml(yaml) as ActionDraftEntry["document"];
      const actions = await tables.actions();
      await actions.save(draft, document);
    } catch (error) {
      return error instanceof Error ? error.message : String(error);
    }
    return null;
  }
  /** Tables whose next version is being started now. */
  private readonly startingTables = new Set<string>();
  /** Idempotency keys of table publishes whose answer never came, by `uses`. */
  private readonly publishKeys = new Map<string, string>();
  /** The step's decision table as step details read and keep it. */
  private stepTables(
    target: string,
    model: ControllerHost["model"],
    opened: number,
  ): StepTables | undefined {
    const tables = this.tables;
    if (!tables) return undefined;
    const current = () => this.host.model === model && model.opened === opened;
    const startOne = async (
      uses: string,
      name: string,
    ): Promise<{ uses: string } | { problem: string }> => {
      const document = this.tableContract(uses);
      if (!document || !current())
        return { problem: `Read ${uses} first: connect to the platform.` };
      const drafts = await tables.drafts();
      const kept = drafts.draft(name);
      if (kept)
        return {
          problem: `${name} ${kept.version} is already a draft on this computer. Choose it in Table to keep editing it.`,
        };
      const listed = (await tables.list().catch(() => []))
        .filter(
          (choice) =>
            choice.where === "published" && choice.uses.startsWith(`${name}@`),
        )
        .map((choice) => choice.uses.slice(name.length + 1));
      if (!current()) return { problem: "The workflow changed; try again." };
      let entry;
      try {
        entry = await drafts.startDraft(
          document as unknown as TableDocument,
          listed,
        );
      } catch (error) {
        return {
          problem: error instanceof Error ? error.message : String(error),
        };
      }
      if (!current()) return { problem: "The workflow changed; try again." };
      const next = `${entry.name}@${entry.version}`;
      this.commit(
        target,
        [{ scope: "step", path: ["uses"], value: next }],
        `ndv:Start ${entry.name} ${entry.version}`,
      );
      return { uses: next };
    };
    return {
      document: (uses) => this.tableContract(uses),
      where: (uses) => tables.where(uses),
      newTable: async () => {
        const step = this.step(target);
        if (!step || !current() || this.readOnlyReason()) return null;
        const published = await tables.list().catch(() => []);
        const drafts = await tables.drafts();
        if (!current()) return null;
        const workflow = String(model.definition.metadata?.name ?? "workflow");
        const taken = new Set(
          published.map((choice) =>
            choice.uses.slice(0, choice.uses.lastIndexOf("@")),
          ),
        );
        let entry;
        try {
          entry = await drafts.newDraft(workflow, target, taken);
        } catch (error) {
          this.host.notify(
            error instanceof Error ? error.message : String(error),
            undefined,
            "danger",
          );
          return null;
        }
        if (!current()) return null;
        const uses = `${entry.name}@${entry.version}`;
        await this.commit(
          target,
          [{ scope: "step", path: ["uses"], value: uses }],
          "ndv:New table",
        );
        return uses;
      },
      startDraft: async (uses) => {
        const name = uses.slice(0, uses.lastIndexOf("@"));
        // One start at a time per table: two would both pass the check below.
        if (this.startingTables.has(name))
          return { problem: `${name}'s next version is being started.` };
        this.startingTables.add(name);
        try {
          return await startOne(uses, name);
        } finally {
          this.startingTables.delete(name);
        }
      },
      publish: async (uses) => {
        const host = this.host;
        const at = uses.lastIndexOf("@");
        const name = uses.slice(0, at);
        const version = uses.slice(at + 1);
        if (!host.profile) return { problem: CONNECT_TO_PUBLISH };
        if (!host.can("definition.publish"))
          return { problem: ROLE_CANT_PUBLISH };
        const drafts = await tables.drafts();
        const entry = drafts.draft(name);
        if (!entry || entry.version !== version)
          return { problem: `${uses} isn't a draft on this computer.` };
        const api = host.api;
        const key = this.publishKeys.get(uses) ?? crypto.randomUUID();
        this.publishKeys.set(uses, key);
        try {
          await publishTable(api, entry.document, key);
        } catch (error) {
          const failure = publishFailure(error);
          if (failure.kind === "unknown") return { problem: UNKNOWN_OUTCOME };
          this.publishKeys.delete(uses);
          if (failure.kind === "role") return { problem: ROLE_CANT_PUBLISH };
          if (failure.kind === "refused") return { problem: failure.message };
          // Published by someone else meanwhile: the changes start the next version.
          const listed = (await tables.list().catch(() => []))
            .filter(
              (choice) =>
                choice.where === "published" &&
                choice.uses.startsWith(`${name}@`),
            )
            .map((choice) => choice.uses.slice(name.length + 1));
          const next = nextDraftVersion(version, [...listed, version]);
          await drafts.moveDraft(entry, next);
          const moved = `${name}@${next}`;
          if (current())
            this.commit(
              target,
              [{ scope: "step", path: ["uses"], value: moved }],
              `ndv:Start ${name} ${next}`,
            );
          return {
            moved,
            message: `${name} ${version} was published by someone else. Your changes start ${next}.`,
          };
        }
        this.publishKeys.delete(uses);
        await drafts.remove(name);
        await drafts.copy(entry.document);
        host.cacheDecisionContract(
          uses,
          entry.document as unknown as Record<string, unknown>,
        );
        tables.retry();
        host.notify(`Published ${name} ${version}`);
        return { published: true };
      },
      evaluate: (body) =>
        this.host.api.request("/studio/local/decision/evaluate", "POST", body),
      account: () => tables.account(),
      save: async (uses, spec, account) => {
        if (tables.account() !== account)
          return "The account changed, so this change wasn't kept.";
        const drafts = await tables.drafts();
        if (tables.account() !== account)
          return "The account changed, so this change wasn't kept.";
        const name = uses.slice(0, uses.lastIndexOf("@"));
        const entry = drafts.draft(name);
        if (!entry || `${entry.name}@${entry.version}` !== uses)
          return `${uses} isn't a draft on this computer.`;
        try {
          await drafts.save(entry, spec as unknown as TableSpec);
          return null;
        } catch (error) {
          return error instanceof Error ? error.message : String(error);
        }
      },
    };
  }
  /** The decision tables step details read themselves; null in a shell without one. */
  tables: TableCatalog | null = null;
  /** A table's document: a draft or copy on this computer, or what was read; reading starts when unknown. */
  tableContract(uses: string): Json | null {
    const tables = this.tables;
    if (!tables)
      return (
        (this.host.decisionContracts.get(uses) as Json | undefined) ?? null
      );
    const known = tables.contract(uses);
    if (!known && uses) void tables.load(uses);
    return known;
  }
  /** Changes when what decides an issue's timing changes: a field left, a step revealed, an entry held or dropped. */
  issueMarks(): number {
    return this.marks + this.pendingStore().version;
  }
  private marks = 0;

  // ------------------------------------------------ entries not applied yet
  private readonly pending = new PendingEntries();
  private pendingScope: {
    model: StructuredCanvasAdapter;
    opened: number;
    profile: string;
    account: string | undefined;
    api: ControllerHost["api"];
  } | null = null;
  /**
   * Entries typed but not applied belong to the workflow that is open, with
   * the platform, account and connection it was opened with: anything else
   * forgets them, so no text reaches another workflow or account.
   */
  private pendingStore(): PendingEntries {
    const { model, profile, api } = this.host;
    const scope = this.pendingScope;
    if (
      scope?.model !== model ||
      scope.opened !== model.opened ||
      scope.profile !== canonicalJson(profile) ||
      scope.account !== this.host.stepDataScope ||
      scope.api !== api
    ) {
      this.pendingScope = {
        model,
        opened: model.opened,
        profile: canonicalJson(profile),
        account: this.host.stepDataScope,
        api,
      };
      this.pending.clear();
    }
    return this.pending;
  }
  hold(
    target: string,
    key: string,
    owner: PendingOwner,
    entry: PendingEntry,
  ): void {
    this.pendingStore().hold(target, key, owner, entry);
  }
  held(target: string, key: string, owner: PendingOwner): PendingEntry | null {
    return this.pendingStore().get(target, key, owner);
  }
  holds(target: string, key: string): boolean {
    return this.pendingStore().has(target, key);
  }
  release(target: string, key: string): void {
    this.pendingStore().release(target, key);
  }
  /** The steps that hold entries (each still to be checked against its fields). */
  heldTargets(): string[] {
    return this.pendingStore().targets();
  }
  /** The rows of a list changed in step details: entries in its rows go. */
  bumpRows(target: string, list: string): void {
    this.pendingStore().bumpRows(target, list);
  }
  rowGeneration(target: string, list: string): number {
    return this.pendingStore().rowGeneration(target, list);
  }
  /** A rename made in step details (or its Undo): the step's entries follow it. */
  renamePending(from: string, to: string): void {
    this.pendingStore().rename(from, to);
    // Test data follows the rename: the step's pins and frames.
    this.testData.rename(from, to);
    // A renamed loop keeps its picked item and its count.
    this.iterations.rename(from, to);
  }
  /**
   * Forgets the entries of steps that are gone, so a step made again with
   * the same name never gets them; `open` keeps the step step details show
   * while they follow a rename back.
   */
  prunePending(open?: string): void {
    if (!this.pendingStore().size) return;
    const ids = new Set(this.host.model.nodes().map((node) => node.step.id));
    this.pendingStore().prune(
      (target) => pseudo(target) || target === open || ids.has(target),
    );
  }

  /** Remove option: back to the default, and out of the form. */
  removeOption(target: string, spec: ParamSpec): CommitOutcome {
    const outcome = this.commitChanges(
      target,
      () => resetParam(spec, this.env(target)),
      `${spec.id}:remove`,
    );
    if (outcome.ok) this.addedBy.get(target)?.delete(spec.id);
    return outcome;
  }

  commit(target: string, changes: FormChange[], field: string): CommitOutcome {
    if (!changes.length || this.turnsOffCallable(target, changes))
      return { ok: false, removed: [] };
    return this.commitChanges(target, () => changes, field);
  }
  /**
   * Turning the settings' Callable off removes Called by a workflow, which
   * writes the same `spec.callable`, after the same question.
   */
  private turnsOffCallable(target: string, changes: FormChange[]): boolean {
    const model = this.host.model;
    if (
      target !== "$workflow" ||
      !isJsonObject(model.definition.spec["callable"])
    )
      return false;
    const off = changes.some(
      (change) =>
        change.scope === "workflow" &&
        change.path.length === 2 &&
        change.path[0] === "spec" &&
        change.path[1] === "callable" &&
        !isJsonObject(change.value),
    );
    const called = triggerIntents(model.canvas).find(
      (intent) => intent.kind === "called",
    );
    if (!off || !called || !this.host.removeTrigger) return false;
    void this.host.removeTrigger(called.id);
    return true;
  }

  private commitChanges(
    target: string,
    changesOf: () => FormChange[],
    field: string,
  ): CommitOutcome {
    this.syncSession();
    if (this.readOnlyReason()) return { ok: false, removed: [] };
    const model = this.host.model;
    const key = `${target}:${field}`;
    const burst = this.burst;
    const merge =
      !!burst &&
      burst.key === key &&
      this.now() - burst.at < BURST_MS &&
      burst.revision === model.revision;
    let outcome: CommitOutcome = { ok: false, removed: [] };
    let reasons: string[] = [];
    let moves: VersionMove[] = [];
    // Set inside perform(); typed so the check after it isn't narrowed to null.
    let ended = null as OnErrorWrite | null;
    let held = null as {
      steps: ReadonlySet<string>;
      identities: (id: string) => string | undefined;
    } | null;
    this.host.perform(() => {
      const changes = changesOf();
      if (!changes.length) return;
      const env = this.env(target);
      const form = this.parameters(target);
      const before = formState(form, env);
      let subject = env.subject;
      for (const change of changes) subject = applyChange(subject, change);
      const step = this.step(target);
      const afterStep = subject.step ?? env.step;
      const afterKind: KindContext = {
        ...this.kindContext(subject.workflow),
        ownedAction: (uses) =>
          step && uses === step["uses"] && subject.action
            ? subject.action
            : ((ownedActionsOf(model.canvas)[uses] as unknown as
                | Json
                | undefined) ?? null),
      };
      const afterForm = step
        ? (this.descriptor(target)?.form?.(afterStep, afterKind) ?? form)
        : form;
      const afterEnv: FormEnv = {
        ...env,
        subject,
        step: afterStep,
        kind: afterKind,
      };
      const hidden = hiddenChanges(
        before,
        formState(afterForm, afterEnv),
        afterEnv,
        new Set(),
      );
      // A change that ends On error (a call that stops waiting for its
      // result) removes the setting and its error path in the same step.
      let settled = subject;
      for (const change of hidden.changes)
        settled = applyChange(settled, change);
      ended = settled.step ? endedOnError(settled.step) : null;
      const endChanges: FormChange[] = (ended?.patch ?? []).map((edit) => ({
        scope: "step",
        path: edit.path,
        value: edit.value,
      }));
      // Changes that leave the step as it is write nothing: the file keeps
      // its bytes (a space typed in a formula, a value set to itself).
      // Key order counts: moving rows reorders an object's keys.
      const same = (a: unknown, b: unknown) =>
        a === b || JSON.stringify(a) === JSON.stringify(b);
      const unchanged =
        !!step &&
        !hidden.changes.length &&
        !endChanges.length &&
        same(subject.step, env.subject.step) &&
        same(subject.action, env.subject.action) &&
        same(subject.workflow, env.subject.workflow);
      const apply = () => {
        moves = this.write(target, [
          ...changes,
          ...hidden.changes,
          ...endChanges,
        ]);
      };
      if (unchanged) {
        outcome = { ok: true, removed: [] };
        return;
      }
      // An error path removed here takes its steps' test data, as deleting
      // them on the canvas does: read the steps as they are, for Undo.
      if (env.subject.step?.["errorPath"] !== undefined)
        held = {
          steps: model.stepIdSet(),
          identities: stepIdentities(model.definition),
        };
      if (merge) model.continueEdit(burst!.revision, apply);
      else model.batch(apply);
      outcome = { ok: true, removed: hidden.labels };
      reasons = hidden.reasons;
    });
    if (!outcome.ok) return outcome;
    if (held) {
      // Kept by the test data for an Undo that brings the same steps back.
      const { steps, identities } = held;
      const after = model.stepIdSet();
      this.testData.prune((id) => after.has(id) || !steps.has(id), identities);
    }
    this.burst = { key, revision: model.revision, at: this.now() };
    if (moves.length) this.host.notify(versionMoveText(moves[0]));
    const revision = model.revision;
    const opened = model.opened;
    const undo = {
      label: "Undo",
      run: () =>
        void this.host.undoIfUnchanged(
          () =>
            this.host.model === model &&
            model.opened === opened &&
            model.revision === revision,
        ),
    };
    // The steps an error path held are the larger loss: their toast names them.
    const path = ended && removedPathToast(ended.removed, ended.names);
    if (path) this.host.notify(path.text, undo, "neutral", path.detail);
    else {
      const removed = ended
        ? [...outcome.removed, "On error"]
        : outcome.removed;
      if (removed.length)
        this.host.notify(
          `Removed ${removed.join(", ")}.${reasons.length ? ` ${reasons[0]}.` : ""}`,
          undo,
        );
    }
    return outcome;
  }

  /** The step's (or trigger's) note; an empty one when there is none. */
  note(target: string): StepNote {
    return stepNotesOf(this.host.model.canvas)[target] ?? { text: "" };
  }
  /**
   * Writes a step's note (null or blank text removes it) as one undo step;
   * typing in a burst stays one step, like a field. `field` keeps changes of
   * different parts of the note (its text, Show on canvas) apart. A note the
   * canvas has no room for is refused with a message above step details.
   * Null once the note is saved, otherwise why it wasn't.
   */
  setNote(
    target: string,
    note: StepNote | null,
    field = "$note",
  ): string | null {
    this.syncSession();
    const locked = this.readOnlyReason();
    if (locked) return locked;
    const model = this.host.model;
    let next: CanvasSidecar;
    try {
      next = withStepNote(model.canvas, target, note);
      if (canvasBytes(next) > CANVAS_MAX_BYTES) throw new Error(CANVAS_TOO_BIG);
    } catch (error) {
      const reason = (error as Error).message;
      this.host.notify(reason, undefined, "danger");
      return reason;
    }
    if (next === model.canvas) return null;
    const key = `${target}:${field}`;
    const burst = this.burst;
    const merge =
      !!burst &&
      burst.key === key &&
      this.now() - burst.at < BURST_MS &&
      burst.revision === model.revision;
    let ok = false;
    this.host.perform(() => {
      const apply = () => {
        model.canvas = next;
      };
      if (merge) model.continueEdit(burst!.revision, apply);
      else model.batch(apply);
      ok = true;
    });
    if (!ok) return "Studio couldn't save the note.";
    this.burst = { key, revision: model.revision, at: this.now() };
    return null;
  }

  /**
   * Moves a step that calls its own file action to a slot on another file
   * server, as one undo step: the action follows the slot (shared with
   * steps on that server, copied when other steps keep the old one).
   */
  useFileServer(target: string, slot: string): CommitOutcome {
    this.syncSession();
    if (this.readOnlyReason()) return { ok: false, removed: [] };
    const model = this.host.model;
    const before = String(this.step(target)?.["uses"] ?? "");
    let moved = "";
    let ok = false;
    this.host.perform(() => {
      model.batch(() => {
        moved = moveFileStep(
          model,
          target,
          slot,
          reservedActionNames(
            this.host.catalogContracts,
            this.host.actionVersions,
            model.canvas,
          ),
          (name) => publishedVersionsOf(this.host.actionVersions, name),
        );
        // The recipes live in the canvas sidecar, which has a size cap.
        if (canvasBytes(model.canvas) > CANVAS_MAX_BYTES)
          throw new Error(CANVAS_TOO_BIG);
      });
      ok = true;
    });
    // The next edit is an undo step of its own.
    if (ok) this.burst = null;
    if (ok && moved !== before && nameOfUses(moved) === nameOfUses(before))
      this.host.notify(versionMoveText({ from: before, to: moved }));
    return { ok, removed: [] };
  }

  private write(target: string, changes: FormChange[]): VersionMove[] {
    const choice = this.triggerChoice(target);
    if (choice) {
      this.writeTriggerChoice(choice, changes);
      return [];
    }
    const model = this.host.model;
    const stepEdits: Edit[] = [];
    const workflowEdits: Edit[] = [];
    let recipe: OwnedRecipe | undefined;
    const step = this.step(target);
    const uses = String(step?.["uses"] ?? "");
    const draft = this.actionDraft(uses);
    let draftSpec: Json | null = null;
    for (const change of changes) {
      if (
        change.scope === "action" &&
        draft &&
        !ownedActionsOf(model.canvas)[uses]
      ) {
        // A published action's next version, kept on this computer.
        const subject: FormSubject = {
          step: null,
          workflow: model.definition,
          action: draftSpec ?? (draft.document.spec as Json),
          roots: [],
        };
        draftSpec = applyChange(subject, change).action;
      } else if (change.scope === "action") {
        recipe ??= ownedActionsOf(model.canvas)[uses];
        if (!recipe)
          throw new Error("This step's action isn't one this workflow owns.");
        const owned: FormSubject = {
          step: null,
          workflow: model.definition,
          action: recipe as unknown as Json,
          roots: [],
        };
        recipe = applyChange(owned, change).action as unknown as OwnedRecipe;
      } else if (change.scope === "workflow")
        workflowEdits.push({
          path: change.path,
          value: change.value,
          scope: "workflow",
        });
      else stepEdits.push({ path: change.path, value: change.value });
    }
    if (stepEdits.length || workflowEdits.length) {
      const next = applyEdits(model.definition, target, [
        ...stepEdits,
        ...workflowEdits,
      ]);
      const updated =
        stepEdits.length && step
          ? findStep(next.spec.steps, (candidate) => candidate.id === target)
          : null;
      if (updated) model.update(target, JSON.stringify(updated));
      if (workflowEdits.length) model.updateWorkflow(next);
    }
    if (draft && draftSpec) this.keepActionDraft(draft, draftSpec);
    if (recipe) {
      // The recipe lives in the canvas sidecar, which has a size cap; a refused edit leaves everything as it was.
      const next = withOwnedAction(model.canvas, uses, recipe);
      if (canvasBytes(next) > CANVAS_MAX_BYTES) throw new Error(CANVAS_TOO_BIG);
      model.canvas = next;
    }
    // A published action never changes: the change starts its next version, for every step that calls it.
    return uses && usesOwning(model.canvas, uses)
      ? forkEdited(
          model,
          (name) => publishedVersionsOf(this.host.actionVersions, name),
          new Set([uses]),
        )
      : [];
  }

  /** A trigger choice's settings go to the canvas sidecar; the workflow's input (its payload) to the workflow. */
  private writeTriggerChoice(choice: TriggerIntent, changes: FormChange[]) {
    const model = this.host.model;
    let subject: FormSubject = {
      step: choice as unknown as Step,
      workflow: model.definition,
      action: null,
      roots: [],
    };
    for (const change of changes) subject = applyChange(subject, change);
    if (subject.workflow !== model.definition)
      model.updateWorkflow(subject.workflow);
    const updated = subject.step as unknown as TriggerIntent;
    const next = withTriggerIntents(
      model.canvas,
      triggerIntents(model.canvas).map((intent) =>
        intent.id === choice.id
          ? { ...updated, id: choice.id, kind: choice.kind }
          : intent,
      ),
    );
    if (canvasBytes(next) > CANVAS_MAX_BYTES) throw new Error(CANVAS_TOO_BIG);
    model.canvas = next;
  }

  scopeOptions(): ScopeOptions {
    return {
      actionOutput: (uses) =>
        (
          (this.actionContract(uses) as Record<string, unknown> | null)?.[
            "spec"
          ] as Record<string, unknown> | undefined
        )?.["outputSchema"],
      decisionOutput: (uses) => {
        const contract = this.tableContract(uses);
        return contract &&
          typeof contract === "object" &&
          !Array.isArray(contract)
          ? (contract["spec"] as Record<string, unknown> | undefined)?.[
              "outputSchema"
            ]
          : undefined;
      },
      workflowOutput: (uses) => {
        const contract = this.callSources().contract(uses);
        return contract &&
          typeof contract === "object" &&
          !Array.isArray(contract)
          ? contract["outputSchema"]
          : undefined;
      },
    };
  }
  /** The workflows a call step may call, and the callers of this one, for this window. */
  callSources(): CallSources {
    return callSources(this.host);
  }

  /**
   * Diagnostics of the last check that point into this step, with paths
   * relative to it; `at` is the field path with the data keys inside an
   * expression (`["with", "parameters", "customerId"]`).
   */
  diagnostics(target: string): StepDiagnostic[] {
    const choice = this.triggerChoice(target);
    if (choice) return choiceDiagnostics(choice);
    const result = this.host.diagnostics;
    if (!result) return [];
    const out: StepDiagnostic[] = [];
    for (const d of result.diagnostics) {
      if (typeof d["path"] !== "string") continue;
      const where = locateDiagnostic(
        d["path"],
        this.host.diagnosticsDefinition,
      );
      const mine = pseudo(target)
        ? where.stepId === "$workflow"
        : where.stepId === target;
      if (!mine) continue;
      if (
        target === "$trigger" &&
        !beneath(where.fieldPath, "/spec/inputSchema")
      )
        continue;
      if (
        target === "$end" &&
        !beneath(where.fieldPath, "/spec/output") &&
        !beneath(where.fieldPath, "/spec/outputSchema")
      )
        continue;
      out.push({
        code: String(d.code ?? ""),
        severity: String(d.severity ?? "error"),
        message: d.message,
        path: where.fieldPath,
        at: [...where.field, ...where.dataPath],
        ...(d["suggestedEdit"] !== undefined
          ? { suggestedEdit: d["suggestedEdit"] }
          : {}),
      });
    }
    return out;
  }

  /** The connected environment and whether it takes test calls ("disabled" until its policy is read, or when it can't be). */
  environmentOf(): NdvContext["environment"] {
    const port = connectionsPort(this.host);
    if (!port) return null;
    const policy = port.policyNow();
    return {
      ...port.environment,
      testCalls:
        policy && policy !== "unreadable" ? policy.test_calls : "disabled",
    };
  }
  ndvContext(target: string): NdvContext {
    const step = this.step(target) ?? WORKFLOW_STAND_IN;
    const descriptor = this.descriptor(target);
    const kind = this.kindContext();
    const host = this.host;
    const model = host.model;
    const opened = model.opened;
    const none = { state: "none" as const };
    const ctx: NdvContext = {
      version: 2,
      step,
      workflow: host.model.definition,
      readOnly: !!this.readOnlyReason(),
      // Run mode shows the run's own iteration; picks are the draft's.
      instanceKey: this.viewingRuns()
        ? null
        : this.iterations.instanceKey(step.id),
      environment: this.environmentOf(),
      scope: (fieldPath) =>
        referenceScope(
          host.model.definition,
          step.id,
          pointer(fieldPath),
          this.scopeOptions(),
        ),
      expectedSchema: (fieldPath) =>
        descriptor
          ?.fields(step)
          .find((field) => pointer(field.path) === pointer(fieldPath))
          ?.expectedSchema?.(step, kind) ?? null,
      outputSchema: () => descriptor?.outputSchema(step, kind) ?? null,
      evaluate: (expression, at) =>
        this.previews.preview(expression, at, ctx) ?? { ok: false },
      diagnostics: (fieldPath) =>
        this.diagnostics(target).filter(
          (d) => !fieldPath || beneath(d.path, pointer(fieldPath)),
        ),
      sample: {
        context: (fieldPath) => {
          const input = this.testEvent();
          const steps: Record<string, { output: Json }> = {};
          for (const visible of referenceScope(
            host.model.definition,
            step.id,
            pointer(fieldPath),
            this.scopeOptions(),
          ).steps) {
            const output = this.pickedStepData(visible.id)?.value;
            if (output !== undefined) steps[visible.id] = { output };
          }
          // Inside a loop's body: the picked item of each loop around the step.
          const looped = this.loopData(step.id);
          return input === undefined && !Object.keys(steps).length && !looped
            ? null
            : { input: input ?? {}, steps, ...looped };
        },
        resolvedInput: () => {
          // What the step received in the last execution (an action's input).
          const result = this.lastResult(target);
          if (!result || result.input === undefined) {
            // Before it ran: what its input computes to from the data the
            // earlier steps have (test input, pins, last results).
            const input = step["with"];
            if (!input || typeof input !== "object") return none;
            const computed = ctx.evaluate(input as Expression, ["with"]);
            return computed.ok && computed.value !== undefined
              ? {
                  state: "available" as const,
                  value: computed.value,
                  source: "simulated" as const,
                  stale: null,
                }
              : none;
          }
          const now = this.prints().get(target)?.execution;
          return {
            state: "available" as const,
            value: result.input,
            source: "simulated" as const,
            stale: now === result.execution ? null : ("upstream" as const),
          };
        },
        output: () => {
          const data = this.stepData(target);
          return data ? { state: "available" as const, ...data } : none;
        },
        setPin: async (value) => {
          // Inside a loop's body: for the item picked.
          const message = await this.pin(this.pinKey(target), value, "manual");
          return message === null ? { ok: true } : { ok: false, message };
        },
        clearPin: () => this.unpin(target),
        script: () => this.scriptOf(target),
        setScript: (value) => this.setScript(target, value),
      },
      catalog: {
        actions: async () =>
          host.actionVersions.map((row) => {
            const uses = `${String(row["name"])}@${String(row["version"])}`;
            return { uses, title: uses };
          }),
        contract: async (uses) => this.actionContract(uses),
        workflows: async (filter) => {
          const sources = this.callSources();
          sources.refresh();
          await sources.lock.read();
          if (host.model !== model || model.opened !== opened) return [];
          return workflowRows(
            sources.list(filter?.callableBy ?? model.definition.metadata.name),
          );
        },
        tablesNote: () => (this.tables?.needsSignIn() ? SIGN_IN_TO_LIST : null),
        tables: async () => {
          if (this.tables) {
            const tables = this.tables;
            const rows = await tables.list();
            if (host.model !== model || model.opened !== opened) return [];
            return rows.map(({ uses, title }) => ({ uses, title }));
          }
          if (!host.profile || !host.can("catalog.read")) return [];
          const { profile, api } = host;
          const page = await api.page("decision-tables");
          if (
            host.profile !== profile ||
            host.api !== api ||
            host.model !== model ||
            model.opened !== opened ||
            !host.can("catalog.read")
          )
            return [];
          return page.items.map((row) => {
            const uses = `${String(row["name"])}@${String(row["version"])}`;
            return { uses, title: uses };
          });
        },
      },
      connections: {
        slot: (name) => {
          const slot = getAt(host.model.definition, [
            "spec",
            "connections",
            name,
          ]) as { connector?: string; required?: boolean } | undefined;
          return slot?.connector
            ? { connector: slot.connector, required: slot.required !== false }
            : null;
        },
        devBinding: (slot) => {
          const port = connectionsPort(host);
          const kept = port?.binding(host.model.definition.metadata.name, slot);
          if (!kept) return null;
          return {
            connectionId: kept.connectionId,
            revisionId: kept.revisionId,
            name: kept.name,
            tested: kept.tested ? (kept.tested.ok ? "ok" : "failed") : null,
          };
        },
        bind: async (slot) => connectionsPort(host)?.open(slot),
      },
      edit: (changes, label) => {
        if (host.model !== model || model.opened !== opened) return;
        void this.commit(
          target,
          changes.map((change) => ({
            scope: change.scope ?? "step",
            path: change.path,
            value: change.value,
          })),
          `ndv:${label}`,
        );
      },
      openAddStep: () => undefined,
      openSubNode: () => undefined,
      openParameterComponent: (kind, subNode) => {
        if (host.model !== model || model.opened !== opened) return;
        this.parameterComponent.set({ target, kind, subNode });
      },
      closeParameterComponent: () => {
        if (this.parameterComponent()?.target === target)
          this.parameterComponent.set(null);
      },
      notify: (text, action) => host.notify(text, action),
      tables: this.stepTables(target, model, opened),
      openWorkflow: (uses) => host.openWorkflow?.(uses),
    };
    return ctx;
  }
}
