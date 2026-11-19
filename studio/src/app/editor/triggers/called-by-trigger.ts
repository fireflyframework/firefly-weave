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
// The Called by a workflow trigger's Parameters: who may call this workflow
// (`spec.callable`, which the workflow settings' Callable switch writes too),
// what callers send and read (the workflow's input and result), and the
// published workflows that call it, read from the project's catalog lock.
// Every change is one undo step through the form session.
import {
  afterNextRender,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  inject,
  Injector,
  input,
  signal,
} from "@angular/core";
import { isJsonObject, type JsonObject } from "../../forms/core/json";
import { fieldsOf } from "../../forms/core/resolve";
import { schemaTypes, typeIcon, typeLabel } from "../../forms/core/scope";
import { SchemaDesigner } from "../../forms/ui/schema-designer";
import { Icon } from "../../icon";
import {
  anyCallerQuestion,
  calledBy,
  callerLine,
  callerProblem,
  callersCaption,
  withCaller,
  withoutCaller,
  type Caller,
} from "../calls/called-by";
import type { FormSession } from "../ndv/params/form-session";

type Side = "input" | "result";
interface FieldRow {
  key: string;
  type: string;
  icon: string;
  required: boolean;
}
interface Box {
  side: Side;
  label: string;
  count: string;
  fields: FieldRow[];
}
/** The Called by section: the callers, or why there are none to show. */
type CallersView =
  | { kind: "callers"; callers: Caller[] }
  | {
      kind: "empty";
      title: string;
      detail: string;
      retry?: boolean;
      loading?: boolean;
    };

const SCHEMA_PATH: Record<Side, string[]> = {
  input: ["spec", "inputSchema"],
  result: ["spec", "outputSchema"],
};
const KEEP_ONE =
  "Keep at least one workflow here, or choose Any workflow in this project.";
let sequence = 0;
/**
 * The names Only these workflows held before Any workflow was chosen, by
 * workflow model and opening: choosing Only again in the same session brings
 * them back.
 */
const keptNames = new WeakMap<object, { opened: number; names: string[] }>();

@Component({
  selector: "weave-called-by-trigger",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, SchemaDesigner],
  templateUrl: "./called-by-trigger.html",
  styleUrl: "./called-by-trigger.css",
})
export class CalledByTrigger {
  readonly session = input.required<FormSession>();
  readonly id = `called-by-${++sequence}`;
  /** Only these workflows, chosen before its first name is added. */
  readonly choosingOnly = signal(false);
  readonly adding = signal(false);
  readonly problem = signal("");
  readonly editing = signal<Side | null>(null);
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);

  private workflow() {
    return this.session().host.model.definition;
  }
  name(): string {
    return this.workflow().metadata.name;
  }
  private callable(): unknown {
    return this.workflow().spec["callable"];
  }
  readOnly(): string | null {
    const session = this.session();
    return session.controller.readOnlyReason(session.target);
  }
  names(): string[] {
    const callable = this.callable();
    return isJsonObject(callable) && Array.isArray(callable["allowedCallers"])
      ? callable["allowedCallers"].filter(
          (name): name is string => typeof name === "string",
        )
      : [];
  }
  only(): boolean {
    return this.names().length > 0 || this.choosingOnly();
  }
  /** The project's workflow names, offered while typing a name. */
  known(): string[] {
    const state = this.session().controller.callSources().lock.current();
    if (state.kind !== "ready") return [];
    const own = this.name();
    return [
      ...new Set(
        [...state.lock.workflows.values()].map((locked) => locked.name),
      ),
    ]
      .filter((name) => name !== own && !this.names().includes(name))
      .sort();
  }

  private write(value: JsonObject, announce: string): boolean {
    const session = this.session();
    const kept = session.commit(
      [{ scope: "workflow", path: ["spec", "callable"], value }],
      "callable",
    );
    if (kept) session.announce(announce);
    return kept;
  }
  /** Any workflow: from a list of names it asks first, and an arrow key alone never widens who may call. */
  async chooseAny() {
    this.adding.set(false);
    this.problem.set("");
    const names = this.names();
    if (!names.length) return this.choosingOnly.set(false);
    const session = this.session();
    const model = session.host.model;
    const opened = model.opened;
    // Only these workflows stays chosen behind the question until it's answered.
    const only = this.radio("only");
    if (only) only.checked = true;
    const sure = await session.confirm(anyCallerQuestion(this.name(), names));
    const same =
      this.session() === session &&
      model.opened === opened &&
      this.names().length > 0;
    if (!sure || !same) {
      // Cancel keeps Only these workflows; focus goes back to it once the
      // question has handed focus back to the radio that opened it.
      const kept = this.radio("only");
      if (kept) kept.checked = true;
      setTimeout(() => this.radio("only")?.focus());
      return;
    }
    keptNames.set(model, { opened, names });
    this.choosingOnly.set(false);
    this.write({}, "Any workflow in this project can call this one.");
  }
  chooseOnly() {
    if (this.names().length) return;
    const model = this.session().host.model;
    const kept = keptNames.get(model);
    if (kept && kept.opened === model.opened && kept.names.length) {
      this.write(
        { allowedCallers: [...kept.names] },
        "Only these workflows can call this one again.",
      );
      return;
    }
    this.choosingOnly.set(true);
    this.startAdding();
  }
  private radio(value: "any" | "only") {
    return this.element.nativeElement.querySelector<HTMLInputElement>(
      `input[value='${value}']`,
    );
  }
  startAdding() {
    this.adding.set(true);
    this.problem.set("");
    this.focus(".cb-add-input");
  }
  add(field: HTMLInputElement): boolean {
    const name = field.value.trim();
    if (!name) return false;
    const problem = callerProblem(name, this.names());
    this.problem.set(problem ?? "");
    if (problem) return false;
    if (
      !this.write(
        withCaller(this.callable(), name),
        `${name} can call this workflow.`,
      )
    )
      return false;
    field.value = "";
    this.choosingOnly.set(false);
    return true;
  }
  addKey(event: KeyboardEvent, field: HTMLInputElement) {
    if (event.key === "Enter") {
      event.preventDefault();
      this.add(field);
    } else if (event.key === "Backspace" && !field.value) {
      const names = this.names();
      if (!names.length) return;
      event.preventDefault();
      this.focusChip(names[names.length - 1]);
    } else if (event.key === "Escape" && !field.value) {
      // Escape leaves the field before it would close step details.
      event.stopPropagation();
      this.adding.set(false);
      this.problem.set("");
      if (!this.names().length) this.choosingOnly.set(false);
      this.focus(this.names().length ? ".cb-add" : "input[value='any']");
    }
  }
  remove(name: string) {
    const next = withoutCaller(this.callable(), name);
    if (!next) {
      this.problem.set(KEEP_ONE);
      return;
    }
    this.problem.set("");
    const names = this.names();
    const at = names.indexOf(name);
    if (this.write(next, `${name} can no longer call this workflow.`))
      this.focusChip(names[at + 1] ?? names[at - 1]);
  }
  /** Why Allow it can't add this caller (the list is full), or null. */
  allowProblem(caller: Caller): string | null {
    return callerProblem(caller.name, this.names());
  }
  /** Allow it: the caller joins Only these workflows, and focus moves to its chip. */
  allow(caller: Caller) {
    if (this.allowProblem(caller)) return;
    if (
      this.write(
        withCaller(this.callable(), caller.name),
        `${caller.name} can call this workflow.`,
      )
    )
      this.focusChip(caller.name);
  }

  boxes(): Box[] {
    const spec = this.workflow().spec;
    return (["input", "result"] as const).map((side) => {
      const schema = spec[side === "input" ? "inputSchema" : "outputSchema"];
      const fields = fieldsOf(schema).map((field): FieldRow => {
        const type = typeLabel(field.schema);
        return {
          key: field.key,
          type,
          icon: typeIcon(schemaTypes(field.schema), type),
          required: field.required,
        };
      });
      return {
        side,
        label: side === "input" ? "Input" : "Result",
        count: `${fields.length} ${fields.length === 1 ? "field" : "fields"}`,
        fields,
      };
    });
  }
  schemaOf(side: Side): unknown {
    const schema =
      this.workflow().spec[side === "input" ? "inputSchema" : "outputSchema"];
    return isJsonObject(schema) ? schema : { type: "object" };
  }
  toggleEdit(side: Side) {
    this.editing.update((open) => (open === side ? null : side));
  }
  writeSchema(side: Side, schema: JsonObject) {
    this.session().commit(
      [{ scope: "workflow", path: SCHEMA_PATH[side], value: schema }],
      side === "input" ? "inputSchema" : "outputSchema",
    );
  }

  callers(): CallersView {
    const name = this.name();
    const state = this.session().controller.callSources().lock.current();
    switch (state.kind) {
      case "offline":
        return {
          kind: "empty",
          title: "Connect to a platform to see which workflows call this one.",
          detail:
            "Studio reads the callers from the project's published workflows.",
        };
      case "forbidden":
        return {
          kind: "empty",
          title: "You can't read this project's published workflows here.",
          detail: "Ask an administrator for access to the project's catalog.",
        };
      case "loading":
        return {
          kind: "empty",
          title: "Reading this project's published workflows.",
          detail: "",
          loading: true,
        };
      case "failed":
        return {
          kind: "empty",
          title: "Studio couldn't read this project's published workflows.",
          detail: state.message,
          retry: true,
        };
      case "ready": {
        const callers = calledBy({
          workflow: name,
          callable: this.callable(),
          lock: state.lock,
        });
        return callers.length
          ? { kind: "callers", callers }
          : {
              kind: "empty",
              title: "No published workflow calls this one yet.",
              detail: `Workflows that call ${name} show here once they're published in this project.`,
            };
      }
    }
  }
  readonly line = callerLine;
  readonly caption = callersCaption;
  refresh() {
    void this.session().controller.callSources().lock.refresh();
  }
  canOpen(): boolean {
    return !!this.session().host.openWorkflow;
  }
  open(caller: Caller) {
    this.session().host.openWorkflow?.(`${caller.name}@${caller.version}`);
  }

  private focus(selector: string) {
    afterNextRender(
      () =>
        this.element.nativeElement
          .querySelector<HTMLElement>(selector)
          ?.focus(),
      { injector: this.injector },
    );
  }
  private focusChip(name: string | undefined) {
    if (name === undefined) return this.focus(".cb-add, .cb-add-input");
    this.focus(`[data-caller="${CSS.escape(name)}"] button`);
  }
}
