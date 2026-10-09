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
import { ownedActionsOf, withOwnedAction } from "./owned/owned-store";
import { CANVAS_TOO_BIG, canvasBytes } from "../state/canvas-sidecar";
import { CANVAS_MAX_BYTES } from "../state/test-data";
import type { OwnedRecipe } from "./owned/owned-actions";
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

export type ControllerHost = Pick<
  StepDetailsHost,
  | "model"
  | "perform"
  | "notify"
  | "undo"
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
  readOnlyReason(): string | null {
    if (this.host.model.readonly) return "Fix the source to edit this step.";
    if (this.host.editingLocked)
      return "A simulation is running. Stop it to edit.";
    return null;
  }
  env(target: string): FormEnv {
    return formEnv(
      this.subject(target),
      this.step(target) ?? WORKFLOW_STAND_IN,
      this.kindContext(),
      this.readOnlyReason(),
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
    this.set(this.touchedBy, target).add(id);
  }
  touched(target: string): ReadonlySet<string> {
    this.syncSession();
    return this.touchedBy.get(target) ?? new Set();
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
      const apply = () => this.write(target, [...changes, ...hidden.changes]);
      if (merge) model.continueEdit(burst!.revision, apply);
      else model.batch(apply);
      outcome = { ok: true, removed: hidden.labels };
      reasons = hidden.reasons;
    });
    if (!outcome.ok) return outcome;
    this.burst = { key, revision: model.revision, at: this.now() };
    if (outcome.removed.length) {
      const revision = model.revision;
      const opened = model.opened;
      this.host.notify(
        `Removed ${outcome.removed.join(", ")}.${reasons.length ? ` ${reasons[0]}.` : ""}`,
        {
          label: "Undo",
          run: () => {
            if (
              this.host.model === model &&
              model.opened === opened &&
              model.revision === revision
            )
              this.host.undo();
          },
        },
      );
    }
    return outcome;
  }

  private write(target: string, changes: FormChange[]): void {
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

  /** Diagnostics of the last check that point into this step, with paths relative to it. */
  diagnostics(target: string): {
    code: string;
    severity: string;
    message: string;
    path: string;
    suggestedEdit?: unknown;
  }[] {
    const result = this.host.diagnostics;
    if (!result) return [];
    const out: {
      code: string;
      severity: string;
      message: string;
      path: string;
      suggestedEdit?: unknown;
    }[] = [];
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
        context: () => null,
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
