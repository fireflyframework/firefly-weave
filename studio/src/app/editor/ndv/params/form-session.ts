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
import {
  canonicalJson,
  formatPointer,
  getAt,
  parsePointer,
} from "../../../forms/core/json";
import { describeDiagnostic } from "../../../designer/diagnostic-copy";
import { suggestedChange } from "../../../designer/diagnostics-list";
import type { DialogField, DialogOptions } from "../../../dialog";
import {
  referenceScope,
  WORKFLOW,
  type ReferenceScope,
  type Schema,
  type ScopeEntry,
  type VisibleStep,
} from "../../../forms/core/scope";
import { decode, get } from "../../../forms/core/binding";
import {
  fieldRules,
  requiredMessage,
  shownProblems,
  type RuleProblem,
} from "./field-rules";
import {
  NOT_APPLIED,
  NOT_SAVED,
  fieldFor,
  fixFor,
  type PlacedSpec,
  type StepIssue,
} from "../issues";
import type {
  PendingEntry,
  PendingKind,
  PendingOwner,
} from "../pending-entries";
import { isMappedText } from "../pending-entries";
import { retryProblem, retryShown } from "../settings/settings-rules";
import {
  formState,
  featureReason,
  type FieldEntry,
  type FormState,
} from "./form-model";
import {
  childEntries,
  nearestRoot,
  planDrop,
  rowKey,
  dropFit,
  parseDrag,
  type DragRef,
} from "./drop";
import { dragSchema } from "../panes/views";
import { uiKey } from "../../state/browser-store";
import {
  expressionRoot,
  pathStartsWith,
  WORKFLOW_ROOTS,
  samePath,
} from "./paths";
import {
  ABSENT,
  applyChange,
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
import { connectorOf } from "../kinds/forms/action-forms";
import { pathPlaceholders, recipeOf } from "../owned/owned-actions";
import { FILE_CONNECTORS } from "../owned/common-actions";
import {
  insertPlaceholder,
  placeholderKey,
  reconcilePathParts,
} from "./url-template";
import { normalizeIdentifier } from "./identifiers";
import {
  allSteps,
  newSlotName,
  renamedConnections,
  SLOT_HINT,
  slotsOf,
} from "./slots";
import { resourceProblem } from "./resource";
import { outlineRank, stepsAfter } from "../navigation";
import type { StepDetailsController } from "../step-details-controller";
import type { StepDetailsHost } from "../step-details-host";
import {
  openRequest,
  type HeldFact,
  type StepDetailsRequest,
} from "../step-details-service";
import { previewText } from "../../formula/preview";
import { checkFormula, type CheckScope } from "../../formula/check";
import type { EditorRequest } from "../../formula/codemirror/formula-editor";
import type { CompletionScope } from "../../formula/complete";
import type { ParseOptions } from "../../formula/grammar";
import {
  enclosingLoops,
  stepPointerOf,
  type HostScopeEntry,
} from "../../formula/loops";

export type FormName = "parameters" | "settings";
export interface SessionHooks {
  confirm(options: DialogOptions): Promise<boolean>;
  prompt?(
    options: DialogOptions & { field: DialogField },
  ): Promise<string | null>;
  announce(text: string): void;
  /** Moves focus to a field, switching tab when needed; `open` also opens its choices. */
  focusField?(field: PlacedSpec, open: boolean): void;
}
export interface FieldLine {
  kind: "error" | "warning" | "preview" | "hint" | "reason" | "none";
  text: string;
  fixLabel?: string;
  fix?: () => void;
}
/** The stand-in for Notes, which is no field of the form, in the issues list. */
const NOTES: PlacedSpec = {
  spec: { id: "notes", path: [], type: "multiline", label: "Notes" },
  tab: "settings",
};
const NOTE_KEY = "note";
/** Fixes the field itself offers: Studio shows them only in the issues list. */
const AT_THE_FIELD = new Set(["focus", "connection"]);
const pointer = formatPointer;
const WHOLE_PATH =
  "The path values are mapped as a whole. Edit them with Edit as YAML in the More menu.";

export class FormSession {
  /** Fields switched to Mapped that hold no mapping yet (the data picker is open). */
  private drafts = new Map<string, Path>();
  /** Where a field's editor puts the caret when it next appears (`{{` typed in a Fixed field). */
  private editorRequests = new Map<string, Omit<EditorRequest, "seq">>();

  private readonly model;
  private readonly opened;
  private readonly profile;
  private readonly account;

  constructor(
    readonly host: StepDetailsHost,
    readonly controller: StepDetailsController,
    readonly request: StepDetailsRequest,
    private readonly hooks: SessionHooks,
    private readonly active: () => boolean = () => true,
  ) {
    this.model = host.model;
    this.opened = host.model.opened;
    this.profile = canonicalJson(host.profile);
    this.account = host.stepDataScope;
  }

  isCurrent(): boolean {
    return (
      this.active() &&
      this.profile === canonicalJson(this.host.profile) &&
      this.account === this.host.stepDataScope &&
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
        // A mapping replaces what the Fixed box held that didn't apply.
        if (next.mode === "mapped") this.release(spec);
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
    // Reset replaces what the field held that didn't apply.
    this.release(spec);
    if (spec.type === "urlTemplate")
      return void this.typeUrl(spec, "", this.urlSnapshot(spec));
    const reason = this.resetReason(spec);
    if (reason) return this.host.notify(reason);
    const structural = this.resetCollection(spec);
    if (structural) {
      if (structural.at !== undefined)
        return this.listRemove(structural.collection, structural.at);
      return;
    }
    this.commit(resetChanges(this.subject(), spec), spec.id);
  }
  resetReason(spec: ParamSpec): string | null {
    const live = this.resolve(spec)?.spec;
    if (!live || (live.scope ?? "step") !== "step") return null;
    const structural = this.resetCollection(live);
    if (structural) {
      const { collection, at } = structural;
      if (at !== undefined) return this.removeReason(collection, at);
      if (this.atDefault(collection)) return null;
      const rows =
        this.structure(collection) === "branches"
          ? this.keyed(collection).map(([key]) => key)
          : this.list(collection).map((_, index) => index);
      if (
        rows.some((row) => {
          const steps = getAt(this.controller.step(this.target), [
            ...collection.path,
            row,
            "steps",
          ]);
          return Array.isArray(steps) && steps.length;
        })
      )
        return "Move or delete its steps first.";
      const control =
        this.structure(collection) === "branches" ? "branch" : "path";
      return `Use Add ${control} or a ${control}'s Remove button.`;
    }
    if (
      this.controller.step(this.target)?.kind === "switch" &&
      samePath(live.path, ["default"])
    ) {
      const steps = getAt(this.controller.step(this.target), [
        "default",
        "steps",
      ]);
      if (Array.isArray(steps) && steps.length)
        return "Move or delete its steps first.";
    }
    return null;
  }
  private resetCollection(spec: ParamSpec): {
    collection: ParamSpec;
    at?: number | string;
  } | null {
    if ((spec.scope ?? "step") !== "step") return null;
    const fields = this.state("parameters").fields;
    const candidates = fields.flatMap(({ spec: field }) => {
      if (field.type !== "fields" || field.path.length) return [field];
      const children = this.children(field);
      return [
        field,
        ...children.shown.map((entry) => entry.spec),
        ...children.collapsed,
      ];
    });
    const collection = candidates.find(
      (field) =>
        (field.scope ?? "step") === "step" &&
        ["cases", "branches"].includes(this.structure(field) ?? "") &&
        samePath(spec.path.slice(0, field.path.length), field.path) &&
        spec.path.length <= field.path.length + 1,
    );
    if (!collection) return null;
    return {
      collection,
      at: spec.path[collection.path.length],
    };
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
    // Conditions choose their data row by row; a URL is always a template
    // whose placeholders carry their own value mode.
    if (spec.type === "conditions" || spec.type === "urlTemplate") return null;
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
        : this.read(spec).mode === "mapped") ||
      this.drafts.has(this.key(spec)) ||
      isMappedText(this.held(spec)?.kind);
    return mapped ? "mapped" : spec.mapping === "both" ? "fixed" : null;
  }

  async setMode(spec: ParamSpec, mode: "fixed" | "mapped"): Promise<void> {
    if (!this.isCurrent() || this.controller.readOnlyReason(this.target))
      return;
    const entry = this.resolve(spec);
    if (!entry || this.readOnly(entry)) return;
    const current = this.read(spec);
    if (mode === "mapped") {
      // Switching to Mapped never asks. A number or JSON that didn't apply
      // waits, out of sight, with the Fixed box it was typed in: back on
      // Fixed it shows again, and only a mapping written over it replaces it.
      this.drafts.set(this.key(spec), spec.path);
      this.host.refreshView();
      return;
    }
    if (current.mode !== "mapped") {
      // Back to the fixed value: template text that didn't apply goes; a
      // number or JSON typed in the Fixed box shows again.
      if (isMappedText(this.heldEntry(spec)?.kind)) this.release(spec);
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
    if (!replace || !owns() || this.controller.readOnlyReason(this.target))
      return;
    const live = this.resolve(spec);
    if (!live || this.readOnly(live)) return;
    this.release(spec);
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

  /** Only live expression destinations can receive a mapping. */
  mappingReason(spec: ParamSpec): string | null {
    const entry = this.resolve(spec);
    if (!this.isCurrent() || !entry)
      return "This field is no longer available.";
    return this.mappingEntryReason(entry);
  }
  private mappingEntryReason(
    entry: FieldEntry,
    appendList = true,
  ): string | null {
    const spec = entry.spec;
    const locked =
      this.controller.readOnlyReason(this.target) ?? this.readOnly(entry);
    if (locked) return locked;
    // Conditions take data in a row: the open rows place it (attachConditionRows).
    if (spec.type === "conditions") return null;
    if (spec.type === "urlTemplate")
      return this.subject().action
        ? this.urlPartsReason()
        : "This field takes a fixed value.";
    if (spec.mapping === "fixed") return "This field takes a fixed value.";
    if (["keyValue", "list"].includes(spec.type) && this.wholeMapping(spec))
      return "Use rows before adding fields.";
    const roots =
      (spec.scope ?? "step") === "workflow"
        ? WORKFLOW_ROOTS
        : this.subject().roots;
    if (
      (spec.scope ?? "step") === "action" ||
      !expressionRoot(spec.path, roots) ||
      this.structure(spec)
    )
      return "This field takes a fixed value.";
    if (spec.type === "list" && appendList) {
      const item = this.appendItem(spec);
      return item
        ? this.mappingEntryReason(item, false)
        : "This list takes fixed values.";
    }
    if (this.mode(spec) === null && spec.type !== "keyValue")
      return "This field takes a fixed value.";
    return null;
  }
  /** A missing row is authorized only through its live parent, never by resolve. */
  private appendItem(list: ParamSpec): FieldEntry | null {
    const item = list.item;
    if (
      !item ||
      !["both", "mapped"].includes(item.mapping ?? "") ||
      item.path.length ||
      (item.scope ?? "step") !== (list.scope ?? "step")
    )
      return null;
    return this.childEntry(this.itemSpec(list, this.list(list).length, item));
  }
  /** Resolve transport hints against the exact target's lexical scope. */
  dragReference(spec: ParamSpec, drag: DragRef): DragRef | null {
    if (
      typeof drag?.ref !== "string" ||
      !parseDrag(JSON.stringify({ ref: drag.ref }))
    )
      return null;
    const live = this.resolve(spec)?.spec;
    // A URL drop fills a path value in the step's input: that is what reads it.
    const target =
      live?.type === "list"
        ? this.appendItem(live)?.spec
        : live?.type === "urlTemplate"
          ? this.urlPartSpec("_")
          : live;
    if (!target) return null;
    const entry = this.scope(target).find((entry) => entry.ref === drag.ref);
    if (!entry) return null;
    return {
      ref: entry.ref,
      breadcrumb: [
        entry.source === "input" ? "Input" : entry.stepId,
        ...entry.path,
      ].join(" › "),
      schema: dragSchema(entry.schema),
    };
  }
  dropFit(spec: ParamSpec, drag: DragRef) {
    if (spec.type === "conditions") {
      const drop = this.conditionDrop(spec, drag);
      return "reason" in drop
        ? { fit: "refused" as const, text: drop.reason }
        : { fit: "fits" as const, text: "Fits" };
    }
    const reason = this.mappingReason(spec);
    if (reason) return { fit: "refused" as const, text: reason };
    const reference = this.dragReference(spec, drag);
    if (!reference)
      return {
        fit: "refused" as const,
        text: "This field cannot read that data.",
      };
    if (spec.type === "urlTemplate") {
      const part = this.urlPartSpec(this.urlKey(spec, reference.ref));
      return dropFit(part, "fixed", this.expected(part), reference, false);
    }
    if (!["keyValue", "list"].includes(spec.type)) {
      const plan = planDrop(spec, this.read(spec), reference, {
        caret: null,
        templates: this.templates(spec),
        mode: this.mode(spec),
      });
      if (plan.kind === "refuse")
        return { fit: "refused" as const, text: plan.reason };
    }
    return dropFit(
      spec,
      this.mode(spec),
      this.expected(spec),
      reference,
      this.templates(spec),
    );
  }
  applyDrop(
    spec: ParamSpec,
    drag: DragRef,
    options: { caret: number | null; shift?: boolean },
  ): boolean {
    if (spec.type === "conditions") {
      const drop = this.conditionDrop(spec, drag);
      if ("reason" in drop) {
        this.announce(drop.reason);
        return false;
      }
      const rows = this.conditionRows.get(this.key(spec));
      if (rows) return rows(drop.entry);
      this.announce("Open the path's conditions to map data into them.");
      return false;
    }
    const reason = this.mappingReason(spec);
    if (reason) {
      this.announce(reason);
      return false;
    }
    const reference = this.dragReference(spec, drag);
    if (!reference) {
      this.announce("This field cannot read that data.");
      return false;
    }
    if (spec.type === "urlTemplate")
      return this.dropUrl(spec, reference, options.caret);
    if (spec.type === "keyValue")
      return this.dropRows(spec, reference, !!options.shift);
    if (spec.type === "list") {
      if (this.wholeMapping(spec)) {
        this.announce("Use rows before adding an item.");
        return false;
      }
      const ok = this.setList(spec, (items) => [
        ...items,
        { mode: "mapped", expression: { ref: reference.ref } },
      ]);
      if (ok) this.mapped(spec, reference);
      return ok;
    }
    const plan = planDrop(spec, this.read(spec), reference, {
      caret: options.caret,
      templates: this.templates(spec),
      mode: this.mode(spec),
    });
    if (plan.kind === "refuse") {
      this.announce(plan.reason);
      return false;
    }
    if (!this.write(spec, plan.next, `${spec.id}:drop:${++this.rowOperation}`))
      return false;
    this.mapped(spec, reference);
    if (plan.replaced !== null) {
      const revision = this.model.revision;
      this.host.notify(
        `Replaced ${plan.replaced} with ${reference.breadcrumb}.`,
        {
          label: "Undo",
          run: () =>
            void this.host.undoIfUnchanged(
              () =>
                this.host.model === this.model &&
                this.model.opened === this.opened &&
                this.model.revision === revision &&
                this.profile === canonicalJson(this.host.profile) &&
                this.account === this.host.stepDataScope,
            ),
        },
      );
    } else this.tipOnce();
    return true;
  }
  private mapped(spec: ParamSpec, drag: DragRef) {
    const fit = dropFit(
      spec,
      this.mode(spec),
      this.expected(spec),
      drag,
      this.templates(spec),
    );
    this.announce(
      `Mapped ${drag.breadcrumb} to ${spec.label}${fit.fit === "mismatch" ? `. ${fit.text}.` : ""}`,
    );
  }
  private dropRows(spec: ParamSpec, drag: DragRef, all: boolean): boolean {
    if (this.wholeMapping(spec)) {
      this.announce("Use rows before adding fields.");
      return false;
    }
    const taken = new Set(this.keyed(spec).map(([key]) => key));
    const children = all ? childEntries(this.scope(spec), drag.ref) : [];
    const refs = children.length
      ? children.map((entry) => entry.ref)
      : [drag.ref];
    const rows = refs.map((ref): [string, ParamValue] => {
      const key = rowKey(ref, taken);
      taken.add(key);
      return [key, { mode: "mapped", expression: { ref } }];
    });
    if (!this.setKeyed(spec, (entries) => [...entries, ...rows])) return false;
    this.announce(
      rows.length === 1
        ? `Mapped ${drag.breadcrumb} to ${spec.label}`
        : `Added ${rows.length} fields to ${spec.label}`,
    );
    this.tipOnce();
    return true;
  }
  addAllFields(spec: ParamSpec): boolean {
    if (spec.type !== "keyValue" || this.mappingReason(spec)) return false;
    if (this.wholeMapping(spec)) {
      this.announce("Use rows before adding fields.");
      return false;
    }
    const scope = this.scope(spec);
    const taken = new Set(this.keyed(spec).map(([key]) => key));
    const rows: [string, ParamValue][] = [];
    for (const entry of childEntries(scope, nearestRoot(scope))) {
      const key = entry.path.at(-1) ?? entry.label;
      if (taken.has(key)) continue;
      taken.add(key);
      rows.push([key, { mode: "mapped", expression: { ref: entry.ref } }]);
    }
    if (!rows.length) {
      this.announce("Every field is already here.");
      return false;
    }
    if (!this.setKeyed(spec, (entries) => [...entries, ...rows])) return false;
    this.announce(`Added ${rows.length} fields to ${spec.label}`);
    return true;
  }
  /** Map to offers collection additions and their visible expression leaves. */
  mapTargets(): ParamSpec[] {
    const out: ParamSpec[] = [];
    const visit = (entry: FieldEntry, depth = 0) => {
      if (depth > 32 || this.readOnly(entry)) return;
      const spec = entry.spec;
      if (spec.type === "fields" && !this.wholeMapping(spec)) {
        this.children(spec).shown.forEach((child) => visit(child, depth + 1));
      } else if (!this.mappingReason(spec)) out.push(spec);
      if (spec.type === "urlTemplate")
        this.urlParts(spec).forEach((part) =>
          visit(this.childEntry(part), depth + 1),
        );
      if (spec.type === "list" && spec.item && !this.wholeMapping(spec)) {
        const indices =
          this.structure(spec) === "branches"
            ? this.keyed(spec).map(([key]) => key)
            : this.list(spec).map((_, i) => i);
        indices.forEach((index) =>
          visit(
            this.childEntry(this.itemSpec(spec, index, spec.item!)),
            depth + 1,
          ),
        );
      }
      if (spec.type === "keyValue" && !this.wholeMapping(spec))
        this.keyed(spec).forEach(([key]) =>
          visit(this.childEntry(this.keyedSpec(spec, key)), depth + 1),
        );
    };
    this.state("parameters").fields.forEach((entry) => visit(entry));
    return out;
  }
  private tipOnce() {
    try {
      const key = uiKey("weave.ndv.dropTip.v1");
      if (localStorage.getItem(key)) return;
      localStorage.setItem(key, "1");
    } catch {
      return;
    }
    this.host.notify(
      "Tip: focus a field in Input and press Enter to map it without dragging.",
    );
  }

  // ------------------------------------------- the URL and conditions
  /** The path of Call an API's URL, as its owned action keeps it. */
  urlPath(url: ParamSpec): string {
    const value = this.read(url);
    return value.mode === "fixed" && typeof value.value === "string"
      ? value.value
      : "";
  }
  /** A placeholder's value: a text field in the step's input under with.path. */
  private urlPartSpec(name: string): ParamSpec {
    return {
      id: `url.path.${name}`,
      path: ["with", "path", name],
      type: "text",
      label: name,
      required: true,
      mapping: "both",
    };
  }
  /** Why the path's values can't be set one by one: the input or its path part is mapped as a whole. */
  urlPartsReason(): string | null {
    const keyed = (path: Path) => {
      const probe: ParamSpec = {
        id: "probe",
        path,
        type: "keyValue",
        label: "",
      };
      const entries = this.entries(probe);
      // A literal that isn't an object reads as having no entries.
      return (
        entries.kind === "object" ||
        (entries.kind === "absent" && this.read(probe).mode === "absent")
      );
    };
    return keyed(["with"]) && keyed(["with", "path"]) ? null : WHOLE_PATH;
  }
  /** The fields of the path's placeholders, one per name in path order. */
  urlParts(url: ParamSpec): ParamSpec[] {
    if (url.type !== "urlTemplate" || this.urlPartsReason()) return [];
    return [...new Set(pathPlaceholders(this.urlPath(url)))].map((name) =>
      this.urlPartSpec(name),
    );
  }
  private pathKeys(): string[] {
    const entries = this.entries({
      id: "probe",
      path: ["with", "path"],
      type: "keyValue",
      label: "",
    });
    return entries.kind === "object" ? entries.keys : [];
  }
  /** The values of the path's placeholders now, for an edit of the path to keep or bring back. */
  urlSnapshot(url: ParamSpec): Map<string, ParamValue> {
    const held = new Set(this.pathKeys());
    return new Map(
      this.urlParts(url)
        .filter((part) => held.has(String(part.path[2])))
        .map((part) => [String(part.path[2]), this.read(part)]),
    );
  }
  /**
   * Typing the path: the action's path, and the values of placeholders the
   * edit removed (or brought back), in one undo step. Values the path didn't
   * use when editing began are left as they are.
   */
  typeUrl(
    url: ParamSpec,
    text: string,
    snapshot: ReadonlyMap<string, ParamValue>,
  ): boolean {
    if (url.type !== "urlTemplate" || !this.editable(url)) return false;
    const writes: [ParamSpec, ParamValue][] = [
      [url, { mode: "fixed", value: text }],
    ];
    if (!this.urlPartsReason()) {
      const { remove, restore } = reconcilePathParts(
        snapshot,
        text,
        new Set(this.pathKeys()),
      );
      writes.push(
        ...remove.map((key): [ParamSpec, ParamValue] => [
          this.urlPartSpec(key),
          ABSENT,
        ]),
        ...restore.map(([key, value]): [ParamSpec, ParamValue] => [
          this.urlPartSpec(key),
          value,
        ]),
      );
    }
    return this.commitWrites(writes, url.id);
  }
  /** A name for the dropped data that neither the path nor the input uses. */
  private urlKey(url: ParamSpec, ref: string): string {
    return placeholderKey(
      ref,
      new Set([...pathPlaceholders(this.urlPath(url)), ...this.pathKeys()]),
    );
  }
  /** A drop on the URL: a placeholder at the caret, filled with the dropped data, in one undo step. */
  private dropUrl(
    url: ParamSpec,
    drag: DragRef,
    caret: number | null,
  ): boolean {
    const key = this.urlKey(url, drag.ref);
    const part = this.urlPartSpec(key);
    const ok = this.commitWrites(
      [
        [
          url,
          {
            mode: "fixed",
            value: insertPlaceholder(this.urlPath(url), caret, key),
          },
        ],
        [part, { mode: "mapped", expression: { ref: drag.ref } }],
      ],
      `${url.id}:drop:${++this.rowOperation}`,
    );
    if (!ok) return false;
    const fit = dropFit(part, "fixed", this.expected(part), drag, false);
    this.announce(
      `Mapped ${drag.breadcrumb} to ${url.label}${fit.fit === "mismatch" ? `. ${fit.text}.` : ""}`,
    );
    this.tipOnce();
    return true;
  }
  /** Writes made one after another, so each sees the ones before it, committed as one edit. */
  private commitWrites(
    writes: [ParamSpec, ParamValue][],
    field: string,
  ): boolean {
    try {
      let subject = this.subject();
      const changes: FormChange[] = [];
      for (const [spec, value] of writes)
        for (const change of writeParam(subject, spec, value)) {
          changes.push(change);
          subject = applyChange(subject, change);
        }
      return this.commit(changes, field);
    } catch (error) {
      if (error instanceof FormWriteError) {
        this.host.notify(error.message);
        return false;
      }
      throw error;
    }
  }
  private conditionRows = new Map<string, (entry: ScopeEntry) => boolean>();
  /**
   * The rows on screen for a decision's conditions: they hold unfinished
   * conditions of their own, so data mapped into the field goes through them
   * (the first empty row, else a new one). Returns the detach function.
   */
  attachConditionRows(
    spec: ParamSpec,
    rows: (entry: ScopeEntry) => boolean,
  ): () => void {
    const key = this.key(spec);
    this.conditionRows.set(key, rows);
    return () => {
      if (this.conditionRows.get(key) === rows) this.conditionRows.delete(key);
    };
  }
  /** The data a drop on a decision's conditions would test, or why it can't. */
  conditionDrop(
    spec: ParamSpec,
    drag: DragRef,
  ): { entry: ScopeEntry } | { reason: string } {
    const gone = { reason: "This field is no longer available." };
    if (spec.type !== "conditions" || !this.isCurrent()) return gone;
    const frozen = this.controller.readOnlyReason(this.target);
    if (frozen) return { reason: frozen };
    const live = this.resolve(spec);
    if (!live) return gone;
    const locked = this.readOnly(live);
    if (locked) return { reason: locked };
    const entry =
      typeof drag?.ref === "string"
        ? this.scope(live.spec).find((option) => option.ref === drag.ref)
        : undefined;
    return entry ? { entry } : { reason: "This field cannot read that data." };
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
  /** What a formula at this field may use: the target's features and the loops around the step. */
  formulaOptions(spec: ParamSpec): ParseOptions {
    void spec;
    return {
      features: this.controller.features(),
      loops: enclosingLoops(this.host.model.definition, this.target),
    };
  }
  /** The host's entries for a field in a loop body; null outside loops or while loading. */
  private hostEntries(spec: ParamSpec): HostScopeEntry[] | null {
    const definition = this.host.model.definition;
    if (!enclosingLoops(definition, this.target).length) return null;
    const step = stepPointerOf(definition, this.target);
    if (!step) return null;
    const { field } = this.rootPointer(spec);
    return (
      this.controller.loopScopes.entries(definition, `${step}${field}`) ?? []
    );
  }
  /** What the field can read: the host's answer inside a loop body, the offline scope elsewhere. */
  formulaScope(spec: ParamSpec): ReferenceScope {
    const hosted = this.hostEntries(spec);
    if (!hosted) {
      const { stepId, field } = this.rootPointer(spec);
      return referenceScope(
        this.host.model.definition,
        stepId,
        field,
        this.controller.scopeOptions(),
      );
    }
    const entries = hosted.filter(
      (entry) => entry.source === "input" || entry.source === "step",
    ) as unknown as ScopeEntry[];
    const steps: VisibleStep[] = hosted
      .filter((entry) => entry.source === "step" && entry.path.length === 0)
      .map((entry) => {
        const id = entry.ref
          .split("/")[2]
          .replace(/~1/g, "/")
          .replace(/~0/g, "~");
        return { id, kind: this.kindOf(id), schema: entry.schema };
      });
    return { found: true, evaluated: true, steps, entries, truncated: false };
  }
  private kindOf(id: string): string {
    let kind = "action";
    const visit = (node: unknown): void => {
      if (Array.isArray(node)) node.forEach(visit);
      else if (node && typeof node === "object") {
        const record = node as Record<string, unknown>;
        if (record["id"] === id && typeof record["kind"] === "string")
          kind = record["kind"];
        else Object.values(record).forEach(visit);
      }
    };
    visit((this.host.model.definition as { spec?: unknown }).spec);
    return kind;
  }
  /** The loop roots' entries (item, index, loops.<id>), inside loop bodies. */
  loopEntries(spec: ParamSpec): HostScopeEntry[] {
    return (this.hostEntries(spec) ?? []).filter(
      (entry) => entry.source === "item" || entry.source === "index",
    );
  }
  completionScope(spec: ParamSpec): CompletionScope {
    const scope = this.formulaScope(spec);
    const options = this.formulaOptions(spec);
    return {
      entries: scope.entries,
      steps: scope.steps,
      loops: options.loops,
      loopEntries: this.loopEntries(spec),
      features: options.features,
      inputSchema: ((
        this.host.model.definition.spec as Record<string, unknown>
      )["inputSchema"] ?? {}) as Schema,
    };
  }
  checkScope(spec: ParamSpec): CheckScope {
    const ids = new Set<string>();
    const visit = (node: unknown): void => {
      if (Array.isArray(node)) node.forEach(visit);
      else if (node && typeof node === "object") {
        const record = node as Record<string, unknown>;
        if (
          typeof record["id"] === "string" &&
          typeof record["kind"] === "string"
        )
          ids.add(record["id"]);
        Object.values(record).forEach(visit);
      }
    };
    visit(
      (this.host.model.definition as { spec?: { steps?: unknown } }).spec
        ?.steps,
    );
    return {
      scope: this.formulaScope(spec),
      stepIds: ids,
      inputSchema: ((
        this.host.model.definition.spec as Record<string, unknown>
      )["inputSchema"] ?? {}) as Schema,
      loopEntries: this.loopEntries(spec),
    };
  }
  requestEditor(spec: ParamSpec, request: Omit<EditorRequest, "seq">): void {
    this.editorRequests.set(this.key(spec), request);
  }
  takeEditorRequest(spec: ParamSpec): Omit<EditorRequest, "seq"> | null {
    const key = this.key(spec);
    const request = this.editorRequests.get(key) ?? null;
    this.editorRequests.delete(key);
    return request;
  }
  /** The first warning the formula at this field gets from the schemas around it. */
  formulaWarning(spec: ParamSpec): string | null {
    const value = this.read(spec);
    if (value.mode !== "mapped") return null;
    return (
      checkFormula(value.expression, this.checkScope(spec))[0]?.message ?? null
    );
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
  /**
   * The field's own problems as they show now (required ones wait on a new
   * step). A read-only field has none to show: its value is set elsewhere,
   * and it says why it can't change instead.
   */
  problems(spec: ParamSpec, readOnly: string | null = null): RuleProblem[] {
    if (readOnly) return [];
    const all = fieldRules(spec, this.read(spec), this.entries(spec));
    return shownProblems(spec.id, all, this.timing());
  }
  private timing() {
    return {
      fresh: this.request.fresh,
      touched: this.controller.touched(this.target),
      revealAll:
        this.request.revealAll || this.controller.revealed(this.target),
    };
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
    const reason = entry ? this.readOnly(entry) : null;
    // The field's own rule speaks first; it needs no Fix: the field is here.
    const problem = this.problems(spec, reason)[0];
    if (problem) return { kind: problem.severity, text: problem.message };
    // A compiler message, a builder problem or a missing input placed here.
    const issue = this.issues().find(
      (item) =>
        !item.key.startsWith("rule:") &&
        !item.status &&
        item.field?.spec.id === spec.id &&
        samePath(item.field.spec.path, spec.path),
    );
    if (issue)
      return this.withFix({ kind: issue.severity, text: issue.message }, issue);
    const warning = this.formulaWarning(spec);
    if (warning) return { kind: "warning", text: warning };
    const value = this.read(spec);
    if (value.mode === "mapped") {
      const preview = this.controller
        .ndvContext(this.target)
        .evaluate(value.expression, spec.path);
      if (preview?.ok) {
        const shown = previewText(preview.value ?? null);
        return {
          kind: "preview",
          text: `= ${shown.text.replace(/\s*\n\s*/g, " ")}${shown.truncated ? "…" : ""}`,
        };
      }
    }
    if (reason) return { kind: "reason", text: reason };
    if (spec.type === "connection")
      return { kind: "hint", text: spec.hint ?? SLOT_HINT };
    return spec.hint
      ? { kind: "hint", text: spec.hint }
      : { kind: "none", text: "" };
  }

  /** A message's Fix at the field, when the field itself doesn't already offer it. */
  private withFix(line: FieldLine, issue: StepIssue): FieldLine {
    const fix = issue.fix;
    if (!fix || AT_THE_FIELD.has(fix.kind)) return line;
    return { ...line, fixLabel: fix.label, fix: () => this.runFix(issue) };
  }

  // ------------------------------------------------------------ issues
  private placedCache: { key: unknown[]; specs: PlacedSpec[] } | null = null;
  private issueCache: { key: unknown[]; issues: StepIssue[] } | null = null;
  /** What issues depend on: when none of it changed, they are the same. */
  private issueKey(): unknown[] {
    const host = this.host;
    return [
      host.model,
      host.model.opened,
      host.model.revision,
      host.model.canvas,
      host.tick?.(),
      host.diagnostics,
      host.diagnosticsDefinition,
      host.contractIssues,
      host.catalogContracts,
      this.controller.ownedProblems(),
      this.controller.features(),
      this.controller.issueMarks(),
      this.controller.readOnlyReason(this.target),
      this.controller.added(this.target).size,
      this.expanded.size,
      this.drafts.size,
      this.structureRevision,
    ];
  }
  private static same(a: unknown[], b: unknown[]): boolean {
    return a.length === b.length && a.every((value, i) => value === b[i]);
  }
  /** Every field shown on both tabs (groups, rows and URL values opened), with its tab. */
  placedSpecs(): PlacedSpec[] {
    const key = this.issueKey();
    if (this.placedCache && FormSession.same(this.placedCache.key, key))
      return this.placedCache.specs;
    const out: PlacedSpec[] = [];
    const visit = (
      entry: FieldEntry,
      tab: FormName,
      inherited: string | null,
      depth: number,
    ) => {
      if (depth > 32) return;
      const readOnly = this.readOnly(entry) ?? inherited;
      out.push({ spec: entry.spec, tab, readOnly });
      for (const child of this.shownChildren(entry.spec))
        visit(child, tab, readOnly, depth + 1);
    };
    for (const entry of this.state("parameters").fields)
      visit(entry, "parameters", null, 0);
    for (const entry of this.state("settings").fields)
      visit(entry, "settings", null, 0);
    this.placedCache = { key, specs: out };
    return out;
  }
  /** The fields a field shows inside it, as the form draws them. */
  private shownChildren(field: ParamSpec): FieldEntry[] {
    if (this.wholeMapping(field) || this.mode(field) === "mapped") return [];
    if (field.type === "fields") {
      if (field.id === "retry" && field.scope === "action") {
        const step = this.controller.step(this.target);
        if (
          !step ||
          !retryShown(step, this.controller.kindContext(), this.fixedOf(field))
        )
          return [];
      }
      return this.children(field).shown;
    }
    if (!this.collectionRows(field)) return [];
    if (field.type === "list" && field.item) {
      const indices =
        this.structure(field) === "branches"
          ? this.keyed(field).map(([key]) => key)
          : this.list(field).map((_, i) => i);
      return indices.map((index) =>
        this.childEntry(this.itemSpec(field, index, field.item!)),
      );
    }
    if (field.type === "keyValue")
      return this.keyed(field).map(([key]) =>
        this.childEntry(this.keyedSpec(field, key)),
      );
    if (field.type === "urlTemplate")
      return this.urlParts(field).map((part) => this.childEntry(part));
    return [];
  }
  private fixedOf(spec: ParamSpec): unknown {
    const value = this.read(spec);
    return value.mode === "fixed" ? value.value : undefined;
  }
  /**
   * The step's issues, fields first in the order they show: field rules
   * (with their timing), entries that don't apply yet, compiler messages,
   * problems the action builder found, missing required inputs, and
   * answers that choose no path.
   */
  issues(): StepIssue[] {
    const key = this.issueKey();
    if (this.issueCache && FormSession.same(this.issueCache.key, key))
      return this.issueCache.issues;
    const specs = this.placedSpecs();
    const timing = this.timing();
    const issues: StepIssue[] = [];
    const ruled = new Set<string>();
    const fieldKey = (placed: PlacedSpec) =>
      `${placed.spec.id}\n${pointer(placed.spec.path)}`;
    for (const placed of specs) {
      for (const problem of this.problems(
        placed.spec,
        placed.readOnly ?? null,
      )) {
        issues.push({
          key: `rule:${fieldKey(placed)}:${problem.code}`,
          severity: problem.severity,
          message: problem.message,
          field: placed,
          fix:
            problem.code === "required"
              ? fixFor("required", null, placed)
              : { kind: "focus", label: "Fix" },
        });
        ruled.add(fieldKey(placed));
      }
      if (placed.spec.id === "retry" && placed.spec.scope === "action") {
        const problem = retryProblem(this.fixedOf(placed.spec));
        const longest = specs.find(
          (other) => other.spec.id === "retry.maxDelaySeconds",
        );
        if (problem && longest) {
          issues.push({
            key: "rule:retry:waits",
            severity: "error",
            message: problem,
            field: longest,
            fix: { kind: "focus", label: "Fix" },
          });
          ruled.add(fieldKey(longest));
        }
      }
    }
    // What the person typed that doesn't apply yet.
    for (const placed of specs) {
      const held = this.held(placed.spec);
      if (!held) continue;
      issues.push({
        key: `held:${fieldKey(placed)}`,
        severity: "error",
        message: held.message,
        status: NOT_APPLIED,
        field: placed,
        fix: { kind: "focus", label: "Fix" },
      });
      ruled.add(fieldKey(placed));
    }
    const note = this.heldNote();
    if (note)
      issues.push({
        key: "held:notes",
        severity: "error",
        message: note.message,
        status: NOT_SAVED,
        field: NOTES,
        fix: { kind: "focus", label: "Fix" },
      });
    const pseudo = this.target.startsWith("$");
    const scoped = specs.filter(
      (placed) =>
        (placed.spec.scope ?? "step") === (pseudo ? "workflow" : "step"),
    );
    const definition = this.host.diagnosticsDefinition;
    this.controller.diagnostics(this.target).forEach((diagnostic, index) => {
      // Notes aren't issues: the canvas doesn't count them either.
      if (diagnostic.severity === "info") return;
      const field = fieldFor(diagnostic.at, scoped);
      if (
        /REQUIRED$/.test(diagnostic.code) &&
        field &&
        timing.fresh &&
        !timing.revealAll &&
        !timing.touched.has(field.spec.id)
      )
        return;
      const message = describeDiagnostic(diagnostic).text;
      if (
        field &&
        issues.some(
          (item) =>
            item.field === field &&
            item.message === message &&
            item.key.startsWith("rule:"),
        )
      )
        return;
      issues.push({
        key: `compiler:${index}`,
        severity: diagnostic.severity === "warning" ? "warning" : "error",
        message,
        field,
        fix: fixFor(
          diagnostic.code,
          suggestedChange(diagnostic.suggestedEdit, definition),
          field,
        ),
      });
    });
    const step = this.controller.step(this.target);
    const uses = String(step?.["uses"] ?? "");
    const actionSpecs = specs.filter(
      (placed) => placed.spec.scope === "action",
    );
    (this.controller.ownedProblems().get(uses) ?? []).forEach(
      (problem, index) => {
        const at = parsePointer(String(problem.path ?? "")) ?? [];
        const field =
          fieldFor(at, actionSpecs) ??
          (at[0] === "pathTemplate"
            ? (specs.find((placed) => placed.spec.type === "urlTemplate") ??
              null)
            : null);
        // The field's own message already says what is wrong there.
        if (field && ruled.has(fieldKey(field))) return;
        issues.push({
          key: `owned:${index}`,
          severity: problem.severity === "warning" ? "warning" : "error",
          message: problem.message,
          field,
          fix: null,
        });
      },
    );
    (this.host.contractIssues ?? [])
      .filter((gap) => gap.stepId === this.target)
      .forEach((gap, index) => {
        const field = fieldFor(
          gap.field === "with" ? ["with", ...gap.dataPath] : ["connection"],
          scoped,
        );
        if (field && ruled.has(fieldKey(field))) return;
        // A missing required input waits like the field's own required message.
        if (
          field &&
          timing.fresh &&
          !timing.revealAll &&
          !timing.touched.has(field.spec.id)
        )
          return;
        issues.push({
          key: `gap:${index}`,
          severity: "error",
          message: field
            ? requiredMessage(field.spec)
            : gap.field === "connection"
              ? "Choose a connection."
              : `Map required field ${gap.label}.`,
          field,
          fix: fixFor("required", null, field),
        });
      });
    const node = this.host.model
      .nodes()
      .find((item) => item.step.id === this.target);
    if (node?.answerPrompt)
      issues.push({
        key: "answers",
        severity: "warning",
        message: "Its answers don't choose a path yet.",
        field: null,
        fix: { kind: "answers", label: "Add paths for answers" },
      });
    // Fields in the order they show, then what belongs to the step as a whole.
    const order = (issue: StepIssue) =>
      issue.field === NOTES
        ? specs.length
        : issue.field
          ? specs.indexOf(issue.field)
          : specs.length + 1;
    const sorted = issues
      .map((issue, index) => ({ issue, index }))
      .sort((a, b) => order(a.issue) - order(b.issue) || a.index - b.index)
      .map(({ issue }) => issue);
    this.issueCache = { key, issues: sorted };
    return sorted;
  }
  /**
   * The fields holding entries that don't apply yet, named for the toast on
   * leaving, with "Not saved" for a note.
   */
  heldEntries(): { label: string; status?: string }[] {
    return this.issues()
      .filter((issue) => issue.status && issue.field)
      .map((issue) => ({
        label: this.entryLabel(issue.field!),
        ...(issue.status === NOT_APPLIED ? {} : { status: issue.status }),
      }));
  }
  /** Labels of the fields holding entries that don't apply yet. */
  heldLabels(): string[] {
    return this.heldEntries().map((entry) => entry.label);
  }
  /**
   * A field's name where a list could make it ambiguous: a row names its
   * list and the row ("Accepted status codes (row 2)", "Headers (X-Trace)"),
   * and a field inside a row names the row ("Path result (path 1)").
   */
  private entryLabel(placed: PlacedSpec): string {
    const { spec } = placed;
    const scope = spec.scope ?? "step";
    let list: ParamSpec | null = null;
    for (const { spec: other } of this.placedSpecs())
      if (
        (other.type === "list" || other.type === "keyValue") &&
        (other.scope ?? "step") === scope &&
        other.path.length < spec.path.length &&
        pathStartsWith(spec.path, other.path) &&
        (!list || other.path.length > list.path.length)
      )
        list = other;
    if (!list) return spec.label;
    const row = spec.path[list.path.length];
    if (spec.path.length > list.path.length + 1)
      return typeof row === "number"
        ? `${spec.label} (${(list.item?.label ?? "row").toLowerCase()} ${row + 1})`
        : `${spec.label} (${row})`;
    return typeof row === "number"
      ? `${list.label} (row ${row + 1})`
      : `${list.label} (${row})`;
  }
  runFix(issue: StepIssue): void {
    const fix = issue.fix;
    if (!fix || !this.isCurrent()) return;
    switch (fix.kind) {
      case "suggestion":
        void this.host.applySuggestion(fix.change);
        return;
      case "answers":
        void this.host.branchOnDecision(this.target);
        return;
      case "update":
        window.open(fix.href, "_blank", "noopener");
        return;
      case "picker": {
        const field = issue.field;
        if (!field) return;
        void this.setMode(field.spec, "mapped").then(() => {
          if (this.isCurrent()) this.hooks.focusField?.(field, true);
        });
        return;
      }
      default:
        if (issue.field)
          this.hooks.focusField?.(issue.field, fix.kind === "connection");
    }
  }

  // ------------------------------------------- entries not applied yet
  private listKey(path: Path, scope: ParamSpec["scope"]): string {
    return `${scope ?? "step"}:${pointer(path)}`;
  }
  /**
   * Entries other steps still hold, checked against their fields as each
   * step's own form would show them, in the workflow's order.
   */
  heldInOtherSteps(): { label: string; step: string; status?: string }[] {
    if (!this.isCurrent()) return [];
    const rank = outlineRank(this.host.model.definition);
    return this.controller
      .heldTargets()
      .filter((target) => target !== this.target)
      .sort((a, b) => rank(a) - rank(b))
      .flatMap((target) =>
        this.reopened(target)
          .heldEntries()
          .map((entry) => ({ ...entry, step: target })),
      );
  }
  /**
   * What stays not applied once step details close, for the toast: this
   * step's entries as its form shows them when it opens again (a field
   * switched to Mapped is Fixed again then, with its entry), then the other
   * steps' entries in the workflow's order.
   */
  heldOnLeaving(): { label: string; step?: string; status?: string }[] {
    if (!this.isCurrent()) return [];
    return [
      ...this.reopened(this.target).heldEntries(),
      ...this.heldInOtherSteps(),
    ];
  }
  /**
   * Every step's entries that don't apply yet, checked against their fields
   * once the workflow changed: an entry that no longer belongs to its field
   * or the value it was typed over goes now (reading one never drops it),
   * and the rest are listed per step in its form's order, for the canvas.
   */
  static heldByStep(
    host: StepDetailsHost,
    controller: StepDetailsController,
  ): Map<string, HeldFact[]> {
    const held = new Map<string, HeldFact[]>();
    for (const target of controller.heldTargets()) {
      const session = new FormSession(host, controller, openRequest(target), {
        confirm: async () => false,
        announce: () => undefined,
      });
      session.pruneHeld();
      const facts = session
        .issues()
        .filter((issue) => issue.status && issue.field)
        .map((issue) => ({
          key: issue.key,
          label: session.entryLabel(issue.field!),
          status: issue.status!,
        }));
      if (facts.length) held.set(target, facts);
    }
    return held;
  }
  /** Drops this step's entries that no longer belong to their field or its value. */
  pruneHeld(): void {
    if (!this.isCurrent() || this.controller.readOnlyReason(this.target))
      return;
    const stale = (key: string, owner: () => PendingOwner) =>
      this.controller.holds(this.target, key) &&
      !this.controller.held(this.target, key, owner());
    for (const { spec } of this.placedSpecs()) {
      const key = this.pendingKey(spec);
      if (stale(key, () => this.pendingOwner(spec)))
        this.controller.release(this.target, key);
    }
    if (stale(NOTE_KEY, () => this.noteOwner()))
      this.controller.release(this.target, NOTE_KEY);
  }
  /** A step's form as it shows when step details open on it again. */
  private reopened(target: string): FormSession {
    return new FormSession(this.host, this.controller, openRequest(target), {
      confirm: async () => false,
      announce: () => undefined,
    });
  }
  private pendingKey(spec: ParamSpec): string {
    return `${spec.scope ?? "step"}:${spec.id}:${pointer(spec.path)}`;
  }
  /** The field as described, the value under it and the rows around it. */
  private pendingOwner(spec: ParamSpec): PendingOwner {
    // Each list on the way: its row count, and how often its rows changed.
    const counts: [number, number, number][] = [];
    spec.path.forEach((segment, at) => {
      if (typeof segment !== "number" || at === 0) return;
      const list = spec.path.slice(0, at);
      const entries = this.entries({
        id: "probe",
        type: "list",
        label: "",
        path: list,
        scope: spec.scope,
      });
      counts.push([
        at,
        entries.kind === "array" ? entries.length : -1,
        this.controller.rowGeneration(
          this.target,
          this.listKey(list, spec.scope),
        ),
      ]);
    });
    return {
      descriptor: canonicalJson(spec),
      baseline: canonicalJson(this.read(spec)),
      structure: JSON.stringify(counts),
    };
  }
  /**
   * What a field holds that doesn't apply yet, while it still belongs to the
   * field and the value it was typed over. Nothing shows while the workflow
   * can't change: the entry waits, and is never applied then.
   */
  held(spec: ParamSpec): PendingEntry | null {
    const entry = this.heldEntry(spec);
    // A number or JSON belongs to the Fixed box: while the field is
    // switched to Mapped it waits out of sight, and isn't listed.
    if (
      (entry?.kind === "number" || entry?.kind === "json") &&
      this.drafts.has(this.key(spec))
    )
      return null;
    return entry;
  }
  /** The field's entry, also while it waits out of sight under Mapped. */
  private heldEntry(spec: ParamSpec): PendingEntry | null {
    if (!this.isCurrent() || this.controller.readOnlyReason(this.target))
      return null;
    if (!this.controller.holds(this.target, this.pendingKey(spec))) return null;
    return this.controller.held(
      this.target,
      this.pendingKey(spec),
      this.pendingOwner(spec),
    );
  }
  hold(
    spec: ParamSpec,
    kind: PendingKind,
    text: string,
    message: string,
  ): void {
    if (!this.isCurrent() || this.controller.readOnlyReason(this.target))
      return;
    this.controller.hold(
      this.target,
      this.pendingKey(spec),
      this.pendingOwner(spec),
      {
        kind,
        text,
        message,
        label: spec.label,
      },
    );
  }
  release(spec: ParamSpec): void {
    if (this.isCurrent())
      this.controller.release(this.target, this.pendingKey(spec));
  }
  private noteOwner(): PendingOwner {
    return {
      descriptor: NOTE_KEY,
      baseline: this.controller.note(this.target).text,
      structure: "",
    };
  }
  /** A note the canvas had no room for, while the saved note is the one it was typed over. */
  heldNote(): PendingEntry | null {
    if (!this.isCurrent() || this.controller.readOnlyReason(this.target))
      return null;
    if (!this.controller.holds(this.target, NOTE_KEY)) return null;
    return this.controller.held(this.target, NOTE_KEY, this.noteOwner());
  }
  holdNote(text: string, reason: string): void {
    if (!this.isCurrent()) return;
    this.controller.hold(this.target, NOTE_KEY, this.noteOwner(), {
      kind: "note",
      text,
      message: reason,
      label: "Notes",
    });
  }
  releaseNote(): void {
    if (this.isCurrent()) this.controller.release(this.target, NOTE_KEY);
  }

  /** The action owns its connector; an imported slot is the local fallback. */
  connector(spec: ParamSpec): string | null {
    const step = this.controller.step(this.target);
    const known = step
      ? connectorOf(step, this.controller.kindContext())
      : null;
    const value = this.read(spec);
    return (
      known ??
      (value.mode === "fixed"
        ? slotsOf(this.host.model.definition).find(
            (slot) => slot.name === value.value,
          )?.connector || null
        : null)
    );
  }
  newSlot(spec: ParamSpec, connector: string | null): void {
    if (spec.type !== "connection" || !this.editable(spec)) return;
    if (!connector)
      return this.host.notify(
        "Choose the action first. The slot follows its connector.",
      );
    if (
      connector !== this.connector(spec) ||
      resourceProblem("name", connector)
    )
      return;
    const name = newSlotName(this.host.model.definition, connector);
    const before = this.host.model.revision;
    const accepted = this.commit(
      [
        {
          scope: "workflow",
          path: ["spec", "connections", name],
          value: { connector, required: true },
        },
        ...writeParam(this.subject(), spec, { mode: "fixed", value: name }),
      ],
      `${spec.id}:slot:${name}`,
    );
    if (accepted && this.host.model.revision !== before)
      this.announce(`Declared the slot ${name}.`);
  }
  /**
   * The file servers this Connection field moves between: every one for a
   * step that calls its own file action, whose action follows the slot;
   * null for any other step, which keeps to its connector's slots.
   */
  fileServers(spec: ParamSpec): ReadonlySet<string> | null {
    if (spec.type !== "connection") return null;
    const step = this.controller.step(this.target);
    const recipe = step ? recipeOf(step, this.controller.kindContext()) : null;
    return recipe?.kind === "connector" && FILE_CONNECTORS.has(recipe.connector)
      ? FILE_CONNECTORS
      : null;
  }
  /**
   * Chooses a slot on another file server for a step that calls its own file
   * action; the action follows, in one undo step. False, with nothing
   * written, when the field can't be edited now or the slot isn't on a file
   * server.
   */
  useFileServer(spec: ParamSpec, slot: string): boolean {
    const servers = this.fileServers(spec);
    if (!servers || !this.editable(spec)) return false;
    const chosen = slotsOf(this.host.model.definition).find(
      (item) => item.name === slot,
    );
    if (!chosen || !servers.has(chosen.connector)) return false;
    const ok =
      this.isCurrent() && this.controller.useFileServer(this.target, slot).ok;
    if (ok)
      this.announce(
        `Now uses the slot ${slot}. The fields follow its file server.`,
      );
    return ok;
  }
  createConnection(spec: ParamSpec): void {
    if (
      spec.type !== "connection" ||
      !this.editable(spec) ||
      !this.host.profile ||
      !this.host.can("connection.manage")
    )
      return;
    const value = this.read(spec);
    const name =
      value.mode === "fixed" && typeof value.value === "string"
        ? value.value
        : "";
    this.host.openConnectionDialog(name || undefined);
  }
  async renameSlot(spec: ParamSpec): Promise<void> {
    if (spec.type !== "connection" || !this.editable(spec)) return;
    const value = this.read(spec);
    const from =
      value.mode === "fixed" && typeof value.value === "string"
        ? value.value
        : "";
    const connector = this.connector(spec);
    const owns = this.owns(spec);
    await this.renameSlotFrom(
      from,
      () => owns() && this.editable(spec) && connector === this.connector(spec),
    );
  }
  /**
   * Renames a slot from workflow settings; every step that uses it follows.
   * The slot's name afterwards, or null when nothing was renamed.
   */
  async renameWorkflowSlot(from: string): Promise<string | null> {
    if (!this.isCurrent() || this.controller.readOnlyReason(this.target))
      return null;
    return this.renameSlotFrom(
      from,
      () => this.isCurrent() && !this.controller.readOnlyReason(this.target),
    );
  }
  private async renameSlotFrom(
    from: string,
    valid: () => boolean,
  ): Promise<string | null> {
    if (
      !this.hooks.prompt ||
      !from ||
      !slotsOf(this.host.model.definition).some((slot) => slot.name === from)
    )
      return null;
    const declared = () =>
      canonicalJson(
        (
          this.host.model.definition.spec["connections"] as
            | Record<string, unknown>
            | undefined
        )?.[from],
      );
    const original = declared();
    const conflict = (text: string) => {
      const next = normalizeIdentifier(text);
      if (!next) return "Enter a name.";
      return next !== from &&
        slotsOf(this.host.model.definition).some((slot) => slot.name === next)
        ? `${next} is already a slot.`
        : "";
    };
    const typed = await this.hooks.prompt({
      title: `Rename slot ${from}`,
      message: "Every step that uses this slot follows the new name.",
      confirmLabel: "Rename",
      field: {
        name: "slot",
        label: "Slot name",
        value: from,
        required: true,
        monospace: true,
        validate: conflict,
      },
    });
    const to = typed === null ? "" : normalizeIdentifier(typed);
    if (!to || to === from) return null;
    const current = () => valid() && !conflict(to) && declared() === original;
    if (!current()) return null;
    let accepted = false;
    this.host.perform(() => {
      if (!current()) return;
      this.host.model.batch(() => {
        const document = structuredClone(this.host.model.definition);
        document.spec["connections"] = renamedConnections(
          document.spec["connections"] as Record<string, unknown>,
          from,
          to,
        );
        this.host.model.updateWorkflow(document);
        for (const step of allSteps(this.host.model.definition))
          if (step["connection"] === from)
            this.host.model.update(
              step.id,
              JSON.stringify({ ...step, connection: to }),
            );
      });
      accepted = true;
    });
    if (!accepted) return null;
    this.announce(`Renamed the slot to ${to}.`);
    return to;
  }
  /** The steps that use a slot of the workflow, in the order they are written. */
  slotUsers(name: string): string[] {
    return allSteps(this.host.model.definition)
      .filter((step) => step["connection"] === name)
      .map((step) => step.id);
  }
  /** Removes a slot no step uses, as one undo step; true when it went. */
  removeWorkflowSlot(name: string): boolean {
    if (
      !this.isCurrent() ||
      this.controller.readOnlyReason(this.target) ||
      this.slotUsers(name).length
    )
      return false;
    const connections = this.host.model.definition.spec["connections"];
    if (
      !connections ||
      typeof connections !== "object" ||
      Array.isArray(connections) ||
      !Object.hasOwn(connections, name)
    )
      return false;
    let accepted = false;
    this.host.perform(() => {
      const document = structuredClone(this.host.model.definition);
      const rest = { ...(connections as Record<string, unknown>) };
      delete rest[name];
      if (Object.keys(rest).length) document.spec["connections"] = rest;
      else delete document.spec["connections"];
      this.host.model.updateWorkflow(document);
      accepted = true;
    });
    if (accepted) this.announce(`Removed the slot ${name}.`);
    return accepted;
  }

  // ---------------------------------------------------------- options
  addOption(id: string): void {
    if (
      !this.isCurrent() ||
      this.controller.readOnlyReason(this.target) ||
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
  /** A list item's spec with an absolute path, in its list's scope unless it names its own. */
  itemSpec(
    list: ParamSpec,
    index: number | string,
    item: ParamSpec,
  ): ParamSpec {
    return this.relocate(
      item.scope || !list.scope ? item : { ...item, scope: list.scope },
      [...list.path, index, ...item.path],
    );
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
  ): boolean {
    if (!this.editable(spec)) return false;
    try {
      let allowed = true;
      const changes = writeList(this.subject(), spec, (items) => {
        const next = update(items);
        allowed = this.validCount(spec, next.length);
        return allowed ? next : items;
      });
      if (allowed && this.commit(changes, field)) {
        this.invalidateRows(spec);
        return true;
      }
    } catch (error) {
      if (error instanceof FormWriteError) this.host.notify(error.message);
      else throw error;
    }
    return false;
  }
  setKeyed(
    spec: ParamSpec,
    update: (entries: [string, ParamValue][]) => [string, ParamValue][],
    field = `${spec.id}:rows:${++this.rowOperation}`,
  ): boolean {
    if (!this.editable(spec)) return false;
    try {
      let allowed = true;
      const changes = writeKeyed(this.subject(), spec, (entries) => {
        const next = update(entries);
        allowed = this.validCount(spec, next.length);
        return allowed ? next : entries;
      });
      if (allowed && this.commit(changes, field)) {
        this.invalidateRows(spec);
        return true;
      }
    } catch (error) {
      if (error instanceof FormWriteError) this.host.notify(error.message);
      else throw error;
    }
    return false;
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
      return void this.setList(spec, () =>
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
      return void this.setList(spec, () =>
        this.answers(spec)
          .filter((_, i) => i !== at)
          .map((a) => ({ mode: "fixed", value: a })),
      );
    if (typeof at === "string")
      return void this.setKeyed(spec, (entries) =>
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
      return void this.setKeyed(spec, (entries) => {
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
        !group.expandChildren &&
        !child.feature &&
        !child.readOnly?.(step, env.kind) &&
        !child.required &&
        child.default !== undefined &&
        isDefault(this.subject(), child) &&
        !this.expanded.has(key) &&
        !this.drafts.has(this.key(child)) &&
        !this.controller.holds(this.target, this.pendingKey(child))
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
      if (field.type === "urlTemplate")
        children.push(
          ...this.urlParts(field).map((part) => this.childEntry(part)),
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
      !this.controller.readOnlyReason(this.target)
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
    this.controller.bumpRows(this.target, this.listKey(spec.path, spec.scope));
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
