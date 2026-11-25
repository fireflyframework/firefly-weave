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
// The Platform event trigger's Parameters: which transaction events start the
// workflow, which workflows they may come from, and the input each run starts
// with. They are the definition's `spec.eventTrigger`; every change is one
// undo step through the form session. The event's fields come from the Studio
// host, and what is wrong with the input from the last check.
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
import { fieldsOf } from "../../forms/core/resolve";
import { schemaTypes, typeIcon, typeLabel } from "../../forms/core/scope";
import {
  AnchoredPopover,
  revealPopoverOption,
} from "../../forms/ui/anchored-popover";
import { Icon } from "../../icon";
import type { FormSession } from "../ndv/params/form-session";
import type { Json } from "../ndv/registry";
import { sourceSuggestions, type SourceGroup } from "./event-sources";
import {
  DEFAULT_DECLARATION,
  eventInputState,
  eventProblems,
  INPUT_REFUSED,
  INPUT_UNSURE,
  loopWarning,
  MAX_SOURCES,
  readDeclaration,
  withDeclaration,
  type EventDeclaration,
  type EventInputState,
} from "./event-trigger";
import {
  currentPlatformEvent,
  EVENT_CHOICE_ORDER,
  EVENT_CHOICES,
  EVENT_DESCRIPTIONS,
  PLATFORM_EVENT_INPUT,
  type PlatformEventType,
} from "./platform-event";

interface FieldRow {
  key: string;
  type: string;
  icon: string;
  /** What the event's field is, or that an input's field is required. */
  about: string;
}
interface InputBox {
  title: string;
  icon: string;
  fields: FieldRow[];
}
interface Option {
  id: string;
  name: string;
  /** The name around the text typed, which is drawn bold. */
  before: string;
  match: string;
  after: string;
  aside: string;
}

/** What each of the event's fields is. */
const EVENT_FIELDS: Readonly<Record<string, string>> = {
  type: "Which event happened",
  emitted_at: "When it happened",
  transaction: "The transaction's facts, business key and steps",
  event_id: "The event's ID",
  version: "The version of these fields",
};
/** Between the field and the sheet below it. */
const GAP = 8;
/** The nearest ancestor that scrolls. */
function scrollerOf(element: HTMLElement): HTMLElement | null {
  for (let at = element.parentElement; at; at = at.parentElement)
    if (
      at.scrollHeight > at.clientHeight &&
      /auto|scroll/.test(getComputedStyle(at).overflowY)
    )
      return at;
  return null;
}
let sequence = 0;
/**
 * The names Only these workflows held before Any workflow was chosen, by
 * workflow model and opening: choosing Only again in the same session brings
 * them back.
 */
const keptNames = new WeakMap<object, { opened: number; names: string[] }>();

@Component({
  selector: "weave-event-trigger-details",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [AnchoredPopover, Icon],
  templateUrl: "./event-trigger-details.html",
  styleUrl: "./event-trigger-details.css",
})
export class EventTriggerDetails {
  readonly session = input.required<FormSession>();
  readonly id = `event-trigger-${++sequence}`;
  readonly choices = EVENT_CHOICE_ORDER.map((type) => ({
    type,
    label: EVENT_CHOICES[type].label,
    description: EVENT_DESCRIPTIONS[type],
  }));
  readonly refused = INPUT_REFUSED;
  readonly unsure = INPUT_UNSURE;
  readonly most = MAX_SOURCES;
  /** The name being typed, and what is wrong with it. */
  readonly text = signal("");
  readonly problem = signal("");
  readonly open = signal(false);
  /** The active suggestion, or -1 while the text typed is what Enter adds. */
  readonly active = signal(-1);
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);

  private workflow() {
    return this.session().host.model.definition;
  }
  name(): string {
    return this.workflow().metadata.name;
  }
  declaration(): EventDeclaration {
    return readDeclaration(this.workflow()) ?? DEFAULT_DECLARATION;
  }
  problems(): { events?: string; from?: string } {
    return eventProblems(this.declaration());
  }
  readOnly(): string | null {
    const session = this.session();
    return session.controller.readOnlyReason(session.target);
  }

  // -------------------------------------------------------------- events
  has(type: PlatformEventType): boolean {
    return this.declaration().events.includes(type);
  }
  toggle(type: PlatformEventType, box: HTMLInputElement) {
    const { events, from } = this.declaration();
    this.write(
      {
        events: box.checked
          ? [...events, type]
          : events.filter((item) => item !== type),
        from,
      },
      `events:${type}`,
    );
    // A change that wasn't kept leaves the box as the definition says.
    box.checked = this.has(type);
  }

  /** Each change has a field of its own, so it is an undo step of its own. */
  private write(next: EventDeclaration, field: string): boolean {
    const value = withDeclaration(this.workflow(), next).spec[
      "eventTrigger"
    ] as Json;
    return this.session().commit(
      [{ scope: "workflow", path: ["spec", "eventTrigger"], value }],
      `eventTrigger:${field}`,
    );
  }

  // ----------------------------------------------------------- workflows
  names(): string[] {
    return this.declaration().from ?? [];
  }
  only(): boolean {
    return this.declaration().from !== null;
  }
  full(): boolean {
    return this.names().length >= MAX_SOURCES;
  }
  loop(): string | null {
    return loopWarning(this.declaration(), this.name());
  }
  chooseAny() {
    const { events, from } = this.declaration();
    this.clear();
    if (from === null) return;
    const model = this.session().host.model;
    if (this.write({ events, from: null }, "from:any") && from.length)
      keptNames.set(model, { opened: model.opened, names: from });
  }
  chooseOnly() {
    const { events, from } = this.declaration();
    if (from !== null) return;
    const model = this.session().host.model;
    const kept = keptNames.get(model);
    const names = kept && kept.opened === model.opened ? kept.names : [];
    if (this.write({ events, from: [...names] }, "from:only") && !names.length)
      this.focus(".et-input");
  }
  remove(name: string) {
    const { events } = this.declaration();
    const names = this.names();
    const at = names.indexOf(name);
    if (
      this.write(
        { events, from: names.filter((item) => item !== name) },
        `from:remove:${name}`,
      )
    ) {
      this.session().announce(`Removed ${name}.`);
      this.focusChip(names[at + 1] ?? names[at - 1]);
    }
  }
  private add(typed: string): void {
    const name = typed.trim();
    if (!name) return;
    const { events } = this.declaration();
    const names = this.names();
    if (names.includes(name)) {
      this.problem.set(`${name} is already here.`);
      return;
    }
    const from = [...names, name];
    const problem = eventProblems({ events: ["transaction.in_doubt"], from });
    this.problem.set(problem.from ?? "");
    if (problem.from) return;
    if (!this.write({ events, from }, `from:add:${name}`)) return;
    this.session().announce(`Added ${name}.`);
    this.clear();
  }
  private clear() {
    this.text.set("");
    this.problem.set("");
    this.close();
  }

  // --------------------------------------------------------- suggestions
  private found(): { note: string | null; groups: SourceGroup[] } {
    const sources = this.session().controller.callSources();
    return sourceSuggestions({
      query: this.text(),
      chosen: this.names(),
      own: this.name(),
      lock: sources.lock.current(),
      drafts: sources.drafts(),
    });
  }
  note(): string | null {
    return this.found().note;
  }
  groups(): { label: string; options: Option[] }[] {
    const query = this.text().trim().toLowerCase();
    let index = 0;
    return this.found().groups.map((group) => ({
      label: group.label,
      options: group.items.map((item) => {
        const at = query ? item.name.toLowerCase().indexOf(query) : 0;
        return {
          id: `${this.id}-option-${index++}`,
          name: item.name,
          before: item.name.slice(0, at),
          match: item.name.slice(at, at + query.length),
          after: item.name.slice(at + query.length),
          aside: item.aside,
        };
      }),
    }));
  }
  private options(): Option[] {
    return this.groups().flatMap((group) => group.options);
  }
  /** The list shows only with something in it. */
  showing(): boolean {
    return this.open() && (!!this.note() || this.options().length > 0);
  }
  activeId(): string | null {
    return this.showing() ? (this.options()[this.active()]?.id ?? null) : null;
  }
  typed(value: string) {
    this.text.set(value);
    this.problem.set("");
    this.active.set(-1);
    this.open.set(true);
    this.revealField();
  }
  /** Below 768 px the suggestions are a sheet: what it would cover of the field scrolls up above it. */
  private revealField() {
    if (window.innerWidth >= 768) return;
    const host = this.element.nativeElement;
    // The list is placed as it is drawn: measured a frame later.
    afterNextRender(
      () =>
        requestAnimationFrame(() => {
          const field = host.querySelector<HTMLElement>(".et-field");
          const list = host.querySelector<HTMLElement>(".et-list");
          if (!field || !list?.matches(":popover-open")) return;
          const covered =
            field.getBoundingClientRect().bottom +
            GAP -
            list.getBoundingClientRect().top;
          if (covered > 0) scrollerOf(field)?.scrollBy({ top: covered });
        }),
      { injector: this.injector },
    );
  }
  close() {
    this.open.set(false);
    this.active.set(-1);
  }
  choose(option: Option) {
    this.add(option.name);
    this.focus(".et-input");
  }
  keydown(event: KeyboardEvent) {
    const options = this.options();
    switch (event.key) {
      case "ArrowDown":
      case "ArrowUp": {
        event.preventDefault();
        if (!options.length) return this.open.set(true);
        const down = event.key === "ArrowDown";
        const at = this.showing() ? this.active() : -1;
        this.open.set(true);
        this.active.set(
          at < 0
            ? down
              ? 0
              : options.length - 1
            : (at + (down ? 1 : options.length - 1)) % options.length,
        );
        this.reveal();
        return;
      }
      case "Enter": {
        event.preventDefault();
        const active = this.showing() ? options[this.active()] : undefined;
        this.add(active?.name ?? this.text());
        return;
      }
      case "Escape":
        // Escape closes the suggestions before it would close step details.
        if (!this.showing()) return;
        event.preventDefault();
        event.stopPropagation();
        this.close();
        return;
      case "Backspace": {
        const names = this.names();
        if (this.text() || !names.length) return;
        event.preventDefault();
        this.close();
        this.focusChip(names[names.length - 1]);
      }
    }
  }
  /** Keeps the active suggestion in view. */
  private reveal() {
    afterNextRender(
      () => {
        const list =
          this.element.nativeElement.querySelector<HTMLElement>(".et-list");
        const id = this.activeId();
        const option = id ? document.getElementById(id) : null;
        if (list && option) revealPopoverOption(list, option);
      },
      { injector: this.injector },
    );
  }

  // --------------------------------------------------------------- input
  inputState(): EventInputState {
    const host = this.session().host;
    return eventInputState(
      this.workflow(),
      host.diagnostics && {
        definition: host.diagnosticsDefinition,
        diagnostics: host.diagnostics.diagnostics,
      },
    );
  }
  /** The support code of what the check found at the input. */
  inputCode(): string {
    const found = this.session().host.diagnostics?.diagnostics.find(
      (item) =>
        item["path"] === "/spec/inputSchema" &&
        /(TYPE_MISMATCH|UNKNOWN_COMPATIBILITY)$/.test(String(item.code ?? "")),
    );
    return String(found?.code ?? "");
  }
  private ownFields(): FieldRow[] {
    return fieldsOf(this.workflow().spec["inputSchema"]).map((field) => {
      const type = typeLabel(field.schema);
      return {
        key: field.key,
        type,
        icon: typeIcon(schemaTypes(field.schema), type),
        about: field.required ? "Required" : "",
      };
    });
  }
  /** "2 fields", for what Undo brings back and what runs start with instead. */
  ownCount(): string {
    const count = this.ownFields().length;
    return count ? `${count} ${count === 1 ? "field" : "fields"}` : "";
  }
  private eventFields(): FieldRow[] {
    return fieldsOf(this.session().controller.eventView() ?? {}).map(
      (field) => {
        const type = typeLabel(field.schema);
        return {
          key: field.key,
          type,
          icon: typeIcon(schemaTypes(field.schema), type),
          about: EVENT_FIELDS[field.key] ?? "",
        };
      },
    );
  }
  /** What each run starts with, field by field; null while it can't be listed. */
  box(): InputBox | null {
    const event = this.inputState() === "event";
    const fields = event ? this.eventFields() : this.ownFields();
    if (!fields.length) return null;
    return event
      ? { title: "Platform event", icon: "signal", fields }
      : { title: "Workflow input", icon: "manualTrigger", fields };
  }
  /** The Studio host couldn't say what the event holds; it is asked again after a pause. */
  unread(): boolean {
    return (
      this.inputState() === "event" &&
      currentPlatformEvent(this.session().host.api)?.state === "unavailable"
    );
  }
  reset() {
    const session = this.session();
    const kept = session.commit(
      [
        {
          scope: "workflow",
          path: ["spec", "inputSchema"],
          value: { ...PLATFORM_EVENT_INPUT },
        },
      ],
      "eventTrigger:input",
    );
    if (!kept) return;
    session.announce("The input is the platform event again.");
    this.focus(".et-events input");
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
    if (name === undefined) return this.focus(".et-input");
    this.focus(`[data-source="${CSS.escape(name)}"] button`);
  }
}
