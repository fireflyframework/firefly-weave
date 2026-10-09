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
import { canonicalJson, formatPointer, getAt } from "../../../forms/core/json";
import type { DialogOptions } from "../../../dialog";
import {
  referenceScope,
  WORKFLOW,
  type Schema,
  type ScopeEntry,
} from "../../../forms/core/scope";
import { decode, get } from "../../../forms/core/binding";
import { fieldRules, shownProblems, type RuleProblem } from "./field-rules";
import {
  formState,
  featureReason,
  type FieldEntry,
  type FormState,
} from "./form-model";
import { expressionRoot, WORKFLOW_ROOTS, samePath } from "./paths";
import {
  ABSENT,
  readList,
  readKeyed,
  writeList,
  writeKeyed,
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
import {
  ndvRegistry,
  type FormSpec,
  type ParamSpec,
  type Path,
} from "../registry";
import { stepsAfter } from "../navigation";
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
  private drafts = new Map<string, Path>();

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
    const descriptor = this.schemaSignature(spec);
    const value = canonicalJson(this.read(spec));
    const structureRevision = this.structureRevision;
    const matches = () => {
      const field = this.resolve(spec)?.spec;
      return (
        !!field &&
        this.schemaSignature(field) === descriptor &&
        (typeof spec.choices !== "function" ||
          field.choices === spec.choices) &&
        field.readOnly === spec.readOnly &&
        field.showWhen === spec.showWhen
      );
    };
    return () =>
      this.isCurrent() &&
      this.model.revision === revision &&
      this.structureRevision === structureRevision &&
      canonicalJson(this.read(spec)) === value &&
      matches();
  }

  private schemaSignature(spec: ParamSpec): string {
    const env = this.controller.env(this.target);
    const shape = (field: ParamSpec, depth: number): unknown => ({
      descriptor: field,
      children:
        depth < 32
          ? field
              .children?.(env.step, env.kind)
              .map((child) => shape(child, depth + 1))
          : undefined,
      item: depth < 32 && field.item ? shape(field.item, depth + 1) : undefined,
    });
    return canonicalJson(shape(spec, 0));
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
    if (!this.editable(spec)) return false;
    try {
      const ok = this.commit(writeParam(this.subject(), spec, next), field);
      if (ok) {
        this.drafts.delete(spec.id + pointer(spec.path));
        for (const owner of this.rowKeys.values()) {
          const parent = owner.spec;
          if (parent)
            owner.signature = canonicalJson(
              this.structure(parent) === "branches" ||
                parent.type === "keyValue"
                ? this.keyed(parent)
                : this.list(parent),
            );
        }
      }
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
    if (!this.editable(spec)) return;
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
    const structured = ["fields", "keyValue", "list"].includes(spec.type);
    const mapped =
      (structured
        ? this.wholeMapping(spec)
        : this.read(spec).mode === "mapped") || this.drafts.has(this.key(spec));
    return mapped ? "mapped" : spec.mapping === "both" ? "fixed" : null;
  }

  async setMode(spec: ParamSpec, mode: "fixed" | "mapped"): Promise<void> {
    if (!this.isCurrent() || this.controller.readOnlyReason()) return;
    const entry = this.resolve(spec);
    if (!entry || this.readOnly(entry)) return;
    const current = this.read(spec);
    if (mode === "mapped") {
      this.drafts.set(this.key(spec), spec.path);
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
    const live = this.resolve(spec);
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
    if (
      !this.isCurrent() ||
      this.controller.readOnlyReason() ||
      ![
        ...this.state("parameters").addable,
        ...this.state("settings").addable,
      ].some((option) => option.spec.id === id && !option.disabled)
    )
      return;
    this.controller.addOption(this.target, id);
    this.host.refreshView();
  }
  removeOption(spec: ParamSpec): void {
    if (spec.required || !this.editable(spec)) return;
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
    return this.relocate(item, [...list.path, index, ...item.path]);
  }
  /** A child of a list item (relative paths) with an absolute path. */
  childSpec(child: ParamSpec, base: Path): ParamSpec {
    return this.relocate(child, [...base, ...child.path]);
  }
  private origins = new WeakMap<ParamSpec, Path>();
  private relocate(spec: ParamSpec, path: Path): ParamSpec {
    const absolute = { ...spec, path };
    this.origins.set(absolute, spec.path);
    return absolute;
  }

  // ------------------------------------------------------ lists and rows
  list(spec: ParamSpec): ParamValue[] {
    return readList(this.subject(), spec);
  }
  keyed(spec: ParamSpec): [string, ParamValue][] {
    return readKeyed(this.subject(), spec);
  }
  setList(
    spec: ParamSpec,
    update: (items: ParamValue[]) => ParamValue[],
    field = `${spec.id}:rows:${++this.rowOperation}`,
  ): void {
    if (!this.editable(spec)) return;
    try {
      const next = update(this.list(spec));
      if (!this.validCount(spec, next.length)) return;
      if (
        this.commit(
          writeList(this.subject(), spec, () => next),
          field,
        )
      )
        this.invalidateRows(spec);
    } catch (error) {
      if (error instanceof FormWriteError) this.host.notify(error.message);
      else throw error;
    }
  }
  setKeyed(
    spec: ParamSpec,
    update: (entries: [string, ParamValue][]) => [string, ParamValue][],
    field = `${spec.id}:rows:${++this.rowOperation}`,
  ): void {
    if (!this.editable(spec)) return;
    try {
      const next = update(this.keyed(spec));
      if (!this.validCount(spec, next.length)) return;
      if (
        this.commit(
          writeKeyed(this.subject(), spec, () => next),
          field,
        )
      )
        this.invalidateRows(spec);
    } catch (error) {
      if (error instanceof FormWriteError) this.host.notify(error.message);
      else throw error;
    }
  }
  /** Lists whose structure belongs to the step: a decision's paths, a parallel's branches, a human task's answers. */
  structure(spec: ParamSpec): "cases" | "branches" | "answers" | null {
    const kind = this.controller.step(this.target)?.kind;
    const path = spec.path.join("/");
    if (kind === "switch" && path === "cases") return "cases";
    if (kind === "parallel" && path === "branches") return "branches";
    if (kind === "humanTask" && path === "decisions") return "answers";
    return null;
  }
  private branches(
    operation: "add" | "remove" | "rename" | "up" | "down",
    name?: string,
    replacement?: string,
  ) {
    const host = this.host;
    host.error = "";
    host.perform(() =>
      host.model.editBranches(this.target, operation, name, replacement),
    );
    if (host.error) {
      host.notify(host.error);
      host.error = "";
    }
  }
  private emptyItem(spec: ParamSpec): ParamValue {
    const item = spec.item;
    if (!item) return { mode: "fixed", value: "" };
    if (item.default !== undefined)
      return { mode: "fixed", value: item.default };
    if (item.type === "number") return { mode: "fixed", value: item.min ?? 0 };
    if (item.type === "boolean") return { mode: "fixed", value: false };
    if (item.type === "fields" || item.type === "keyValue")
      return { mode: "fixed", value: {} };
    return { mode: "fixed", value: "" };
  }
  listAdd(spec: ParamSpec): void {
    if (!this.editable(spec)) return;
    const count =
      this.structure(spec) === "branches"
        ? this.keyed(spec).length
        : this.list(spec).length;
    if (!this.validCount(spec, count + 1)) return;
    const structure = this.structure(spec);
    if (structure === "cases" || structure === "branches") {
      this.branches("add");
      this.invalidateRows(spec);
      return;
    }
    if (structure === "answers") {
      const answers = this.answers(spec);
      let n = answers.length + 1;
      while (answers.includes(`answer-${n}`)) n++;
      return this.setList(spec, () =>
        [...answers, `answer-${n}`].map((a) => ({ mode: "fixed", value: a })),
      );
    }
    this.setList(spec, (items) => [...items, this.emptyItem(spec)]);
  }
  private answers(spec: ParamSpec): string[] {
    const value = this.read(spec);
    return value.mode === "fixed" && Array.isArray(value.value)
      ? value.value.map(String)
      : ((spec.default as string[] | undefined) ?? []);
  }
  removeReason(spec: ParamSpec, at: number | string): string | null {
    const structure = this.structure(spec);
    const count =
      structure === "branches"
        ? this.keyed(spec).length
        : this.entries(spec).kind === "array"
          ? this.list(spec).length
          : 0;
    if (spec.minItems !== undefined && count <= spec.minItems)
      return `Keep at least ${spec.minItems}.`;
    if (structure === "cases" || structure === "branches") {
      const steps = getAt(this.controller.step(this.target), [
        ...spec.path,
        at,
        "steps",
      ]);
      if (Array.isArray(steps) && steps.length)
        return "Move or delete its steps first.";
    }
    return null;
  }
  listRemove(spec: ParamSpec, at: number | string): void {
    if (!this.editable(spec)) return;
    const reason = this.removeReason(spec, at);
    if (reason) return this.host.notify(reason);
    const structure = this.structure(spec);
    if (structure === "cases" || structure === "branches")
      return this.branches("remove", String(at));
    if (structure === "answers")
      return this.setList(spec, () =>
        this.answers(spec)
          .filter((_, i) => i !== at)
          .map((a) => ({ mode: "fixed", value: a })),
      );
    if (typeof at === "string")
      return this.setKeyed(spec, (entries) =>
        entries.filter(([key]) => key !== at),
      );
    this.setList(spec, (items) => items.filter((_, i) => i !== at));
  }
  listMove(spec: ParamSpec, from: number, to: number): void {
    if (!this.editable(spec)) return;
    const count =
      spec.type === "keyValue" || this.structure(spec) === "branches"
        ? this.keyed(spec).length
        : this.list(spec).length;
    if (from === to || from < 0 || to < 0 || from >= count || to >= count)
      return;
    if (
      this.structure(spec) === "cases" ||
      this.structure(spec) === "branches"
    ) {
      const step = from < to ? 1 : -1;
      const model = this.host.model;
      this.host.perform(() =>
        model.batch(() => {
          for (let at = from; at !== to; at += step)
            model.editBranches(
              this.target,
              step > 0 ? "down" : "up",
              this.structure(spec) === "branches"
                ? Object.keys(
                    this.controller.step(this.target)!["branches"] as object,
                  )[at]
                : String(at),
            );
        }),
      );
      this.invalidateRows(spec);
      return;
    }
    if (spec.type === "keyValue") {
      return this.setKeyed(spec, (entries) => {
        const next = [...entries];
        const [moved] = next.splice(from, 1);
        next.splice(to, 0, moved);
        return next;
      });
    }
    this.setList(spec, (items) => {
      const next = [...items];
      const [moved] = next.splice(from, 1);
      next.splice(to, 0, moved);
      return next;
    });
  }
  listRename(spec: ParamSpec, from: number | string, next: string): void {
    if (!this.editable(spec) || !next) return;
    const names =
      this.structure(spec) === "answers"
        ? this.answers(spec)
        : this.keyed(spec).map(([key]) => key);
    if (
      names.includes(next) &&
      (typeof from === "string" ? from !== next : names[from] !== next)
    ) {
      this.host.notify(`${next} is already used. Choose another name.`);
      return;
    }
    const structure = this.structure(spec);
    if (structure === "branches")
      return this.branches("rename", String(from), next);
    if (structure === "answers") {
      const old = this.answers(spec)[Number(from)];
      if (!old || old === next) return;
      const host = this.host;
      host.error = "";
      host.perform(() => host.model.renameDecision(this.target, old, next));
      if (host.error) {
        host.notify(host.error);
        host.error = "";
      }
      return;
    }
    if (typeof from === "string")
      this.setKeyed(spec, (entries) =>
        entries.map(
          ([key, value]) =>
            [key === from ? next : key, value] as [string, ParamValue],
        ),
      );
  }
  /** A path card's output chip (its canvas label) and the step that runs first on it. */
  rowInfo(path: Path): { chip: string; next: string } | null {
    const step = this.controller.step(this.target);
    if (!step) return null;
    const container = [...path, "steps"];
    const descriptor = ndvRegistry.kind(step.kind);
    const output = descriptor
      ?.outputs?.(step, this.controller.kindContext())
      .find((o) => o.containerPath && samePath(o.containerPath, container));
    if (!output) return null;
    const steps = getAt(step, container);
    const first =
      Array.isArray(steps) && steps.length
        ? String((steps[0] as { id: string }).id)
        : null;
    return {
      chip: output.label,
      next:
        first ??
        stepsAfter(this.host.model.definition, this.target)[0] ??
        "End",
    };
  }

  // ----------------------------------------------------------- groups
  private expanded = new Map<string, Path>();
  /** A group's children: shown ones, and optional ones at their default as add buttons. */
  children(group: ParamSpec): { shown: FieldEntry[]; collapsed: ParamSpec[] } {
    const env = this.controller.env(this.target);
    const step = env.step;
    const relative = group.children?.(step, env.kind) ?? [];
    const origin = this.origins.get(group);
    const absolute = relative.map((child) => {
      if (origin && samePath(child.path.slice(0, origin.length), origin))
        return this.relocate(child, [
          ...group.path,
          ...child.path.slice(origin.length),
        ]);
      return child.path.length &&
        String(child.path[0]) === String(group.path[0])
        ? child
        : this.childSpec(child, group.path);
    });
    const shown: FieldEntry[] = [];
    const collapsed: ParamSpec[] = [];
    for (const child of absolute) {
      if (child.showWhen && !child.showWhen(step, env.kind)) continue;
      const key = child.id + JSON.stringify(child.path);
      if (
        !child.feature &&
        !child.readOnly?.(step, env.kind) &&
        !child.required &&
        child.default !== undefined &&
        isDefault(this.subject(), child) &&
        !this.expanded.has(key)
      )
        collapsed.push(child);
      else
        shown.push({
          spec: child,
          option: false,
          readOnly: env.readOnly ?? child.readOnly?.(step, env.kind) ?? null,
          disabled:
            child.feature && !env.kind.features.includes(child.feature)
              ? featureReason(child.feature)
              : null,
        });
    }
    return { shown, collapsed };
  }
  expandChild(child: ParamSpec): void {
    if (!this.editable(child, true)) return;
    this.expanded.set(child.id + JSON.stringify(child.path), child.path);
    this.host.refreshView();
  }

  // ---------------------------------------------- mapping as a whole
  wholeMapping(spec: ParamSpec): boolean {
    return this.entries(spec).kind === "mapped";
  }
  mapWhole(spec: ParamSpec): void {
    void this.setMode(spec, "mapped");
  }
  async useRows(spec: ParamSpec): Promise<void> {
    await this.setMode(spec, "fixed");
  }
  /** Resolve only descriptors generated by the live form and its current rows. */
  resolve(spec: ParamSpec, includeCollapsed = false): FieldEntry | null {
    const visit = (entry: FieldEntry, depth: number): FieldEntry | null => {
      if (depth > 32) return null;
      const field = entry.spec;
      if (
        field.id === spec.id &&
        samePath(field.path, spec.path) &&
        canonicalJson(field) === canonicalJson(spec)
      )
        return entry;
      if (this.readOnly(entry)) return null;
      const children: FieldEntry[] = [];
      if (field.type === "fields" && !this.wholeMapping(field)) {
        const parts = this.children(field);
        children.push(...parts.shown);
        if (includeCollapsed)
          children.push(
            ...parts.collapsed.map((child) => this.childEntry(child)),
          );
      }
      if (field.type === "list" && field.item && !this.wholeMapping(field)) {
        const indices =
          this.structure(field) === "branches"
            ? this.keyed(field).map(([key]) => key)
            : this.list(field).map((_, i) => i);
        children.push(
          ...indices.map((index) =>
            this.childEntry(this.itemSpec(field, index, field.item!)),
          ),
        );
      }
      if (field.type === "keyValue" && !this.wholeMapping(field))
        children.push(
          ...this.keyed(field).map(([key]) =>
            this.childEntry(this.keyedSpec(field, key)),
          ),
        );
      for (const child of children) {
        const found = visit(child, depth + 1);
        if (found) return found;
      }
      return null;
    };
    for (const entry of [
      ...this.state("parameters").fields,
      ...this.state("settings").fields,
    ]) {
      const found = visit(entry, 0);
      if (found) return found;
    }
    return null;
  }
  childEntry(spec: ParamSpec): FieldEntry {
    const env = this.controller.env(this.target);
    return {
      spec,
      option: false,
      readOnly: env.readOnly ?? spec.readOnly?.(env.step, env.kind) ?? null,
      disabled:
        spec.feature && !env.kind.features.includes(spec.feature)
          ? featureReason(spec.feature)
          : null,
    };
  }
  keyedSpec(group: ParamSpec, key: string): ParamSpec {
    if (group.item) return this.itemSpec(group, key, group.item);
    const path = [...group.path, key];
    const value = readParam(this.subject(), { path, scope: group.scope });
    const json = value.mode === "fixed" && typeof value.value !== "string";
    return {
      id: `${group.id}.value`,
      path,
      scope: group.scope,
      type: json ? "json" : "text",
      label: "Value",
      mapping: "both",
      default: json ? null : "",
    };
  }
  private editable(spec: ParamSpec, includeCollapsed = false): boolean {
    const entry = this.resolve(spec, includeCollapsed);
    return (
      this.isCurrent() &&
      !!entry &&
      !this.readOnly(entry) &&
      !this.controller.readOnlyReason()
    );
  }
  private validCount(spec: ParamSpec, count: number): boolean {
    const reason =
      spec.maxItems !== undefined && count > spec.maxItems
        ? `Keep at most ${spec.maxItems}.`
        : spec.minItems !== undefined && count < spec.minItems
          ? `Keep at least ${spec.minItems}.`
          : null;
    if (reason) this.host.notify(reason);
    return !reason;
  }
  private rowKeys = new Map<
    string,
    { signature: string; generation: number; spec: ParamSpec }
  >();
  rowKey(spec: ParamSpec, index: number | string): string {
    const key = this.key(spec);
    const signature = canonicalJson(
      this.structure(spec) === "branches" || spec.type === "keyValue"
        ? this.keyed(spec)
        : this.list(spec),
    );
    let owner = this.rowKeys.get(key);
    if (!owner) {
      owner = { signature, generation: 0, spec };
      this.rowKeys.set(key, owner);
    } else if (owner.signature !== signature) {
      this.clearRowState(spec);
      owner.signature = signature;
      owner.generation++;
    }
    return `${key}:${owner.generation}:${index}`;
  }
  private rowOperation = 0;
  private structureRevision = 0;
  private invalidateRows(spec: ParamSpec): void {
    this.structureRevision++;
    this.clearRowState(spec);
    const owner = this.rowKeys.get(this.key(spec));
    if (owner) {
      owner.generation++;
      owner.signature = canonicalJson(
        this.structure(spec) === "branches" || spec.type === "keyValue"
          ? this.keyed(spec)
          : this.list(spec),
      );
    }
    this.renderKeys.clear();
  }
  private clearRowState(spec: ParamSpec): void {
    for (const [key, path] of this.drafts)
      if (
        path.length > spec.path.length &&
        samePath(path.slice(0, spec.path.length), spec.path)
      )
        this.drafts.delete(key);
    for (const [key, path] of this.expanded)
      if (
        path.length > spec.path.length &&
        samePath(path.slice(0, spec.path.length), spec.path)
      )
        this.expanded.delete(key);
  }
  private renderKeys = new Map<string, { descriptor: string; key: object }>();
  fieldKey(entry: FieldEntry): object {
    const descriptor = canonicalJson({
      spec: entry.spec,
      readOnly: entry.readOnly,
      disabled: entry.disabled,
    });
    const id = this.key(entry.spec);
    let cached = this.renderKeys.get(id);
    if (!cached || cached.descriptor !== descriptor) {
      cached = { descriptor, key: {} };
      this.renderKeys.set(id, cached);
    }
    return cached.key;
  }
  /** A queued focus belongs to the initiating live field and connected component. */
  focusLater(
    root: HTMLElement,
    spec: ParamSpec,
    locate: () => HTMLElement | null | undefined,
    alive: () => boolean,
  ): void {
    const owns = this.owns(spec);
    const origin = document.activeElement;
    setTimeout(() => {
      if (
        !alive() ||
        !root.isConnected ||
        !owns() ||
        (origin?.isConnected && document.activeElement !== origin)
      )
        return;
      const target = locate();
      if (
        target &&
        !target.matches(":disabled, [aria-disabled='true']") &&
        target.getClientRects().length &&
        getComputedStyle(target).visibility !== "hidden"
      ) {
        target.focus();
        if (
          target instanceof HTMLInputElement &&
          target.classList.contains("param-kv-name")
        )
          target.select();
      }
    });
  }
  firstControl(root: HTMLElement): HTMLElement | null {
    return (
      [
        ...root.querySelectorAll<HTMLElement>(
          "input, textarea, select, button, [tabindex='0']",
        ),
      ].find(
        (target) =>
          !target.matches(":disabled, [aria-disabled='true']") &&
          target.getClientRects().length &&
          getComputedStyle(target).visibility !== "hidden",
      ) ?? null
    );
  }
  collectionRows(spec: ParamSpec): boolean {
    const value = this.read(spec);
    if (spec.display === "chips" && value.mode !== "absent")
      return (
        value.mode === "fixed" &&
        Array.isArray(value.value) &&
        value.value.every((item) => typeof item === "string")
      );
    if (value.mode === "absent" || value.mode === "mapped") return true;
    return spec.type === "list" && this.structure(spec) !== "branches"
      ? Array.isArray(value.value)
      : value.value !== null &&
          typeof value.value === "object" &&
          !Array.isArray(value.value);
  }
}
