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
// One open form (a step's Parameters or Settings, the trigger's, End's or
// the workflow's): what each field reads, writes, shows under itself and
// offers in its menu. Components read through it; every write goes through
// the controller as an undo step.
import { canonicalJson, formatPointer } from "../../../forms/core/json";
import type { DialogOptions } from "../../../dialog";
import {
  referenceScope,
  WORKFLOW,
  type Schema,
  type ScopeEntry,
} from "../../../forms/core/scope";
import { decode, get } from "../../../forms/core/binding";
import { fieldRules, shownProblems, type RuleProblem } from "./field-rules";
import { formState, type FieldEntry, type FormState } from "./form-model";
import { expressionRoot, WORKFLOW_ROOTS } from "./paths";
import {
  ABSENT,
  FormWriteError,
  isDefault,
  readEntries,
  readParam,
  resetChanges,
  writeParam,
  type Entries,
  type FormChange,
  type FormSubject,
  type ParamValue,
} from "./value-io";
import type { FormSpec, ParamSpec, Path } from "../registry";
import type { StepDetailsController } from "../step-details-controller";
import type { StepDetailsHost } from "../step-details-host";
import type { StepDetailsRequest } from "../step-details-service";

export type FormName = "parameters" | "settings";
export interface SessionHooks {
  confirm(options: DialogOptions): Promise<boolean>;
  announce(text: string): void;
}
export interface FieldLine {
  kind: "error" | "warning" | "preview" | "hint" | "reason" | "none";
  text: string;
  fixLabel?: string;
  fix?: () => void;
}
const pointer = formatPointer;

export class FormSession {
  /** Fields switched to Mapped that hold no mapping yet (the data picker is open). */
  private drafts = new Set<string>();

  private readonly model;
  private readonly opened;

  constructor(
    readonly host: StepDetailsHost,
    readonly controller: StepDetailsController,
    readonly request: StepDetailsRequest,
    private readonly hooks: SessionHooks,
    private readonly active: () => boolean = () => true,
  ) {
    this.model = host.model;
    this.opened = host.model.opened;
  }

  isCurrent(): boolean {
    return (
      this.active() &&
      this.host.model === this.model &&
      this.model.opened === this.opened
    );
  }

  /** Deferred work keeps the value and descriptor it was started for. */
  owns(spec: ParamSpec): () => boolean {
    const revision = this.model.revision;
    const descriptor = canonicalJson(spec);
    const matches = () =>
      [this.form("parameters"), this.form("settings")].some((form) =>
        [...form.fields, ...(form.options ?? [])].some(
          (field) =>
            field.id === spec.id &&
            canonicalJson(field) === descriptor &&
            (typeof spec.choices !== "function" ||
              field.choices === spec.choices) &&
            field.readOnly === spec.readOnly &&
            field.showWhen === spec.showWhen,
        ),
      );
    return () =>
      this.isCurrent() && this.model.revision === revision && matches();
  }

  get target(): string {
    return this.request.target;
  }
  form(name: FormName): FormSpec {
    return name === "settings"
      ? this.controller.settings(this.target)
      : this.controller.parameters(this.target);
  }
  state(name: FormName): FormState {
    return formState(this.form(name), this.controller.env(this.target));
  }
  subject(): FormSubject {
    return this.controller.subject(this.target);
  }
  read(spec: ParamSpec): ParamValue {
    return readParam(this.subject(), spec);
  }
  entries(spec: ParamSpec): Entries {
    return readEntries(this.subject(), spec);
  }
  commit(changes: FormChange[], field: string): boolean {
    return (
      this.isCurrent() && this.controller.commit(this.target, changes, field).ok
    );
  }
  write(spec: ParamSpec, next: ParamValue, field = spec.id): boolean {
    try {
      const ok = this.commit(writeParam(this.subject(), spec, next), field);
      if (ok) this.drafts.delete(spec.id + pointer(spec.path));
      return ok;
    } catch (error) {
      if (error instanceof FormWriteError) {
        this.host.notify(error.message);
        return false;
      }
      throw error;
    }
  }
  clear(spec: ParamSpec): void {
    this.commit(resetChanges(this.subject(), spec), spec.id);
  }
  /** True when the field holds nothing or exactly its default (Reset to default has nothing to do). */
  atDefault(spec: ParamSpec): boolean {
    return isDefault(this.subject(), spec);
  }

  // ------------------------------------------------------- value modes
  private key(spec: ParamSpec) {
    return spec.id + pointer(spec.path);
  }
  mode(spec: ParamSpec): "fixed" | "mapped" | null {
    if (
      spec.mapping === "mapped" ||
      spec.type === "formula" ||
      spec.type === "fileRef"
    )
      return "mapped";
    if (spec.mapping !== "both") return null;
    return this.read(spec).mode === "mapped" || this.drafts.has(this.key(spec))
      ? "mapped"
      : "fixed";
  }
  async setMode(spec: ParamSpec, mode: "fixed" | "mapped"): Promise<void> {
    if (!this.isCurrent() || this.controller.readOnlyReason()) return;
    const entry = this.state("parameters")
      .fields.concat(this.state("settings").fields)
      .find(
        (entry) =>
          entry.spec.id === spec.id &&
          pointer(entry.spec.path) === pointer(spec.path),
      );
    if (entry && this.readOnly(entry)) return;
    const current = this.read(spec);
    if (mode === "mapped") {
      this.drafts.add(this.key(spec));
      this.host.refreshView();
      return;
    }
    if (current.mode !== "mapped") {
      this.drafts.delete(this.key(spec));
      return this.host.refreshView();
    }
    const owns = this.owns(spec);
    const replace = await this.hooks.confirm({
      title: "Replace the mapping with a fixed value?",
      message: "The field goes back to its default. You can undo this.",
      confirmLabel: "Replace",
      cancelLabel: "Keep the mapping",
    });
    if (!replace || !owns() || this.controller.readOnlyReason()) return;
    const live = this.state("parameters")
      .fields.concat(this.state("settings").fields)
      .find(
        (entry) =>
          entry.spec.id === spec.id &&
          pointer(entry.spec.path) === pointer(spec.path),
      );
    if (!live || this.readOnly(live)) return;
    this.drafts.delete(this.key(spec));
    this.commit(
      writeParam(
        this.subject(),
        spec,
        spec.default !== undefined
          ? { mode: "fixed", value: spec.default }
          : this.fixedEmpty(spec),
      ),
      spec.id,
    );
  }
  /** The empty Fixed value of a field: nothing, or an empty group for structured fields. */
  private fixedEmpty(spec: ParamSpec): ParamValue {
    if (spec.type === "fields" || spec.type === "keyValue")
      return { mode: "fixed", value: {} };
    if (spec.type === "list") return { mode: "fixed", value: [] };
    return ABSENT;
  }
  /** Mapped text fields edit templates when the platform has text.concat. */
  templates(spec: ParamSpec): boolean {
    return (
      !!spec.templateCapable &&
      this.controller.features().includes("text.concat")
    );
  }

  // -------------------------------------------------------------- data
  private rootPointer(spec: ParamSpec): { stepId: string; field: string } {
    if ((spec.scope ?? "step") === "workflow") {
      const root = expressionRoot(spec.path, WORKFLOW_ROOTS) ?? spec.path;
      return { stepId: WORKFLOW, field: pointer(root) };
    }
    const root = expressionRoot(spec.path, this.subject().roots) ?? spec.path;
    return { stepId: this.target, field: pointer(root) };
  }
  scope(spec: ParamSpec): ScopeEntry[] {
    const { stepId, field } = this.rootPointer(spec);
    return referenceScope(
      this.host.model.definition,
      stepId,
      field,
      this.controller.scopeOptions(),
    ).entries;
  }
  /** The schema a field's value must fit, when the step's contract says. */
  expected(spec: ParamSpec): Schema | null {
    const subject = this.subject();
    const root = expressionRoot(spec.path, subject.roots);
    if (!root) return null;
    let schema = subject.schemaOf?.(root) as Schema | undefined;
    for (const key of spec.path.slice(root.length)) {
      const properties = schema?.["properties"] as
        | Record<string, Schema>
        | undefined;
      schema =
        properties?.[String(key)] ?? (schema?.["items"] as Schema | undefined);
    }
    return schema ?? null;
  }
  /** The decoded value of an expression part, for chips and pills. */
  node(spec: ParamSpec) {
    const subject = this.subject();
    const root = expressionRoot(spec.path, subject.roots);
    if (!root || !subject.step) return undefined;
    try {
      return get(
        decode(subject.step[String(root[0])] ?? undefined),
        spec.path.slice(1),
      );
    } catch {
      return undefined;
    }
  }

  // ------------------------------------------------------- what shows
  touch(spec: ParamSpec): void {
    this.controller.touch(this.target, spec.id);
  }
  problems(spec: ParamSpec): RuleProblem[] {
    const all = fieldRules(spec, this.read(spec), this.entries(spec));
    return shownProblems(spec.id, all, {
      fresh: this.request.fresh,
      touched: this.controller.touched(this.target),
      revealAll: this.request.revealAll,
    });
  }
  requiredEmpty(spec: ParamSpec): boolean {
    return fieldRules(spec, this.read(spec), this.entries(spec)).some(
      (p) => p.code === "required",
    );
  }
  readOnly(entry: FieldEntry): string | null {
    return entry.readOnly ?? entry.disabled;
  }
  line(spec: ParamSpec, entry: FieldEntry | null): FieldLine {
    const problem = this.problems(spec)[0];
    if (problem) return { kind: problem.severity, text: problem.message };
    const value = this.read(spec);
    if (value.mode === "mapped") {
      const preview = this.controller
        .ndvContext(this.target)
        .evaluate(value.expression, spec.path);
      if (preview?.ok)
        return {
          kind: "preview",
          text: `= ${JSON.stringify(preview.value)}`.slice(0, 200),
        };
    }
    const reason = entry ? this.readOnly(entry) : null;
    if (reason) return { kind: "reason", text: reason };
    return spec.hint
      ? { kind: "hint", text: spec.hint }
      : { kind: "none", text: "" };
  }

  // ---------------------------------------------------------- options
  addOption(id: string): void {
    this.controller.addOption(this.target, id);
    this.host.refreshView();
  }
  removeOption(spec: ParamSpec): void {
    this.controller.removeOption(this.target, spec);
  }
  announce(text: string): void {
    this.hooks.announce(text);
  }
  confirm(options: DialogOptions): Promise<boolean> {
    return this.hooks.confirm(options);
  }

  // ------------------------------------------------------- structure
  /** A list item's spec with an absolute path. */
  itemSpec(
    list: ParamSpec,
    index: number | string,
    item: ParamSpec,
  ): ParamSpec {
    return { ...item, path: [...list.path, index, ...item.path] };
  }
  /** A child of a list item (relative paths) with an absolute path. */
  childSpec(child: ParamSpec, base: Path): ParamSpec {
    return { ...child, path: [...base, ...child.path] };
  }
}
