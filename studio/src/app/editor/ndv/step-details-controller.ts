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
import { signal, untracked } from "@angular/core";
import { safeSample } from "./panes/sample";
import { canonicalJson, getAt } from "../../forms/core/json";
import { referenceScope, type ScopeOptions } from "../../forms/core/scope";
import { locateDiagnostic } from "../../designer/diagnostic-location";
import type { StructuredCanvasAdapter, Step, Workflow } from "../../model";
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
  withStepNote,
  type CanvasSidecar,
  type StepNote,
} from "../state/canvas-sidecar";
import { CANVAS_MAX_BYTES } from "../state/test-data";
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
  type FormSpec,
  type Json,
  type KindContext,
  type NdvContext,
  type ParamSpec,
  type Path,
  type StepKindDescriptor,
} from "./registry";
import type { StepDetailsHost } from "./step-details-host";
import type { HostDiagnostic } from "../../integrations/http-action-client";
import {
  PendingEntries,
  type PendingEntry,
  type PendingOwner,
} from "./pending-entries";
import type { PreviewData } from "../formula/preview";

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
  | "label"
  | "editingLocked"
  | "profile"
  | "stepDataScope"
  | "api"
  | "diagnostics"
  | "diagnosticsDefinition"
  | "cacheDecisionContract"
  | "can"
>;
/** A diagnostic of one step, located in it. */
export interface StepDiagnostic {
  code: string;
  severity: string;
  message: string;
  path: string;
  at: Path;
  suggestedEdit?: unknown;
}
export interface CommitOutcome {
  ok: boolean;
  /** Labels of fields the change hid and cleared. */
  removed: string[];
}

const PSEUDO = new Set(["$trigger", "$end", "$workflow"]);
const pointer = (path: Path) =>
  path
    .map((s) => "/" + String(s).replace(/~/g, "~0").replace(/\//g, "~1"))
    .join("");
const beneath = (path: string, root: string): boolean =>
  path === root || path.startsWith(`${root}/`);
const BURST_MS = 600;

export class StepDetailsController {
  private eventSchema = "";
  private readonly event = signal<Json | undefined>(undefined);
  private eventScope: {
    model: StructuredCanvasAdapter;
    opened: number;
    profile: string;
    account: string | undefined;
    api: ControllerHost["api"];
  } | null = null;
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
    this.eventScope = {
      model,
      opened: model.opened,
      profile: canonicalJson(profile),
      account: this.host.stepDataScope,
      api,
    };
    this.eventSchema = "";
    untracked(() => this.event.set(undefined));
  }
  testEvent(): Json | undefined {
    this.syncTestEvent();
    const schema = this.host.model.definition.spec["inputSchema"];
    const signature = canonicalJson(schema);
    if (this.eventSchema !== signature) {
      this.eventSchema = signature;
      untracked(() => this.event.set(safeSample(schema, this.event())));
    }
    return this.event();
  }
  /** Deferred pane work belongs to the workflow and connection that created it. */
  testEventOwner(): () => boolean {
    this.syncTestEvent();
    const scope = this.eventScope;
    return () => {
      this.syncTestEvent();
      return this.eventScope === scope;
    };
  }
  setTestEvent(value: Json | undefined): void {
    this.syncTestEvent();
    this.event.set(
      safeSample(this.host.model.definition.spec["inputSchema"], value),
    );
  }
  /** What formula previews read: the trigger's test event; step outputs arrive with test data. */
  previewData(): PreviewData {
    return {
      input: () => this.testEvent(),
      output: () => undefined,
      stamp: () => [this.host.model.revision, this.testEvent()],
    };
  }

  readonly features = signal<readonly string[]>([]);
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
        if (current()) this.features.set(manifest.features ?? []);
      } catch {
        if (current()) {
          this.features.set([]);
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
      actionContract: (uses) =>
        (this.host.catalogContracts.get(uses) as Json | undefined) ?? null,
      tableContract: (uses) =>
        (this.host.decisionContracts.get(uses) as Json | undefined) ?? null,
      workflowContract: () => null,
      ownedAction: (uses) =>
        (owned[uses] as unknown as Json | undefined) ?? null,
    };
  }
  step(target: string): Step | null {
    if (PSEUDO.has(target)) return null;
    return (
      this.host.model.nodes().find((node) => node.step.id === target)?.step ??
      null
    );
  }
  descriptor(target: string): StepKindDescriptor | null {
    const step = this.step(target);
    return step ? (ndvRegistry.kind(step.kind) ?? null) : null;
  }
  /** The recipe of the owned action this step calls, else the published action's spec (read-only). */
  private actionOf(step: Step): Json | null {
    const uses = String(step["uses"] ?? "");
    const owned = ownedActionsOf(this.host.model.canvas)[uses];
    if (owned) return owned as unknown as Json;
    const document = this.host.catalogContracts.get(uses);
    return (document?.["spec"] as Json | undefined) ?? null;
  }
  subject(target: string): FormSubject {
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
    return (
      (step &&
        this.descriptor(target)?.settings?.(step, this.kindContext())) || {
        fields: [],
      }
    );
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
      (target) => PSEUDO.has(target) || target === open || ids.has(target),
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
    if (!changes.length) return { ok: false, removed: [] };
    return this.commitChanges(target, () => changes, field);
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
      const apply = () => {
        moves = this.write(target, [...changes, ...hidden.changes]);
      };
      if (merge) model.continueEdit(burst!.revision, apply);
      else model.batch(apply);
      outcome = { ok: true, removed: hidden.labels };
      reasons = hidden.reasons;
    });
    if (!outcome.ok) return outcome;
    this.burst = { key, revision: model.revision, at: this.now() };
    if (moves.length) this.host.notify(versionMoveText(moves[0]));
    if (outcome.removed.length) {
      const revision = model.revision;
      const opened = model.opened;
      this.host.notify(
        `Removed ${outcome.removed.join(", ")}.${reasons.length ? ` ${reasons[0]}.` : ""}`,
        {
          label: "Undo",
          run: () =>
            void this.host.undoIfUnchanged(
              () =>
                this.host.model === model &&
                model.opened === opened &&
                model.revision === revision,
            ),
        },
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
    const model = this.host.model;
    const stepEdits: Edit[] = [];
    const workflowEdits: Edit[] = [];
    let recipe: OwnedRecipe | undefined;
    const step = this.step(target);
    const uses = String(step?.["uses"] ?? "");
    for (const change of changes) {
      if (change.scope === "action") {
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

  scopeOptions(): ScopeOptions {
    return {
      actionOutput: (uses) =>
        (
          this.host.catalogContracts.get(uses)?.["spec"] as
            | Record<string, unknown>
            | undefined
        )?.["outputSchema"],
      decisionOutput: (uses) =>
        (
          this.host.decisionContracts.get(uses)?.["spec"] as
            | Record<string, unknown>
            | undefined
        )?.["outputSchema"],
    };
  }

  /**
   * Diagnostics of the last check that point into this step, with paths
   * relative to it; `at` is the field path with the data keys inside an
   * expression (`["with", "parameters", "customerId"]`).
   */
  diagnostics(target: string): StepDiagnostic[] {
    const result = this.host.diagnostics;
    if (!result) return [];
    const out: StepDiagnostic[] = [];
    for (const d of result.diagnostics) {
      if (typeof d["path"] !== "string") continue;
      const where = locateDiagnostic(
        d["path"],
        this.host.diagnosticsDefinition,
      );
      const mine = PSEUDO.has(target)
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
      instanceKey: null,
      environment: null,
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
        context: () => {
          const input = this.testEvent();
          return input === undefined ? null : { input, steps: {} };
        },
        resolvedInput: () => none,
        output: () => none,
        setPin: async () => ({
          ok: false,
          message: "Pinning output isn't available yet.",
        }),
        clearPin: () => undefined,
        script: () => null,
        setScript: async () => ({
          ok: false,
          message: "Test scripts aren't available yet.",
        }),
      },
      catalog: {
        actions: async () =>
          host.actionVersions.map((row) => {
            const uses = `${String(row["name"])}@${String(row["version"])}`;
            return { uses, title: uses };
          }),
        contract: async (uses) =>
          (host.catalogContracts.get(uses) as Json | undefined) ?? null,
        workflows: async () => [],
        tables: async () => {
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
        devBinding: () => null,
        bind: async () => undefined,
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
    };
    return ctx;
  }
}
