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
// Resource modes keep local text until it is accepted or explicitly replaced.
import {
  AfterViewInit,
  Directive,
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  inject,
  input,
} from "@angular/core";
import { stringify } from "yaml";
import { Modal } from "../../../dialog";
import { canonicalJson } from "../../../forms/core/json";
import { Select, type SelectOption } from "../../../forms/ui/select";
import { Icon } from "../../../icon";
import type { Choice, Json, ParamSpec } from "../registry";
import type { FormSession } from "./form-session";
import {
  MODE_LABELS,
  firstMode,
  resourceModes,
  resourceProblem,
  type ResourceMode,
} from "./resource";
/** The nested viewer escapes the scrolling pane's stacking context. */
@Directive({
  selector: "[weaveResourceViewerLayer]",
  standalone: true,
  host: { popover: "manual" },
})
class ResourceViewerLayer implements AfterViewInit {
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  ngAfterViewInit(): void {
    this.element.nativeElement.showPopover();
  }
}

@Component({
  selector: "weave-resource-field",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, Modal, Select, ResourceViewerLayer],
  template: `@let modes = this.modes();
    <div class="param-resource">
      @if (modes.length > 1) {
        <div
          class="param-segments param-resource-modes"
          role="radiogroup"
          [attr.aria-label]="'Mode for ' + spec().label"
          (keydown)="modeKey($event)"
        >
          @for (choice of modes; track choice) {
            <button
              type="button"
              role="radio"
              [attr.aria-checked]="mode() === choice"
              [attr.tabindex]="mode() === choice ? 0 : -1"
              [disabled]="!!readOnly()"
              (click)="choose(choice)"
            >
              {{ labels[choice] }}
            </button>
          }
        </div>
      }
      <div class="param-resource-row">
        @if (mode() === "list") {
          <weave-select
            [label]="spec().label"
            [hideLabel]="true"
            [controlId]="controlId()"
            [options]="options()"
            [value]="value()"
            [disabled]="!!readOnly()"
            [describedBy]="description()"
            placeholder="Choose from the list"
            (choose)="write($event)"
          />
        } @else {
          <input
            class="param-input"
            autocomplete="off"
            spellcheck="false"
            [id]="controlId()"
            [attr.aria-label]="spec().label"
            [attr.aria-describedby]="description()"
            [attr.aria-invalid]="!!problem() || null"
            [readOnly]="!!readOnly()"
            [placeholder]="placeholder()"
            [value]="text()"
            (input)="edit($event)"
            (change)="typed()"
            (keydown.enter)="typed()"
          />
        }
        @if (document()) {
          <button type="button" class="text-link param-open" (click)="open()">
            <weave-icon name="external" [size]="16" />Open
          </button>
        }
      </div>
      @if (problem()) {
        <p
          class="param-line param-resource-problem"
          [id]="controlId() + '-problem'"
          data-kind="error"
          role="alert"
        >
          <weave-icon name="warning" [size]="16" />{{ problem() }}
        </p>
      }
    </div>
    @if (viewer(); as view) {
      <weave-modal
        class="param-resource-viewer"
        weaveResourceViewerLayer
        [heading]="'View ' + view.name"
        closeLabel="Close"
        [wide]="true"
        (dismiss)="close()"
      >
        <pre
          class="param-viewer"
          tabindex="0"
          [attr.aria-label]="view.name + ' as YAML'"
          >{{ view.yaml }}</pre
        >
        <div class="modal-actions">
          <button type="button" (click)="copy()">
            <weave-icon name="copy" [size]="16" />Copy
          </button>
        </div>
      </weave-modal>
    }`,
})
export class ResourceField {
  session = input.required<FormSession>();
  spec = input.required<ParamSpec>();
  controlId = input.required<string>();
  describedBy = input("");
  readOnly = input<string | null>(null);
  choices = input<readonly Choice[]>([]);
  readonly labels = MODE_LABELS;
  private readonly lifetime = inject(DestroyRef);
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private owner: {
    session: FormSession;
    descriptor: string;
    baseline: string;
  } | null = null;
  private picked: ResourceMode | null = null;
  private draft: { text: string; mode: ResourceMode } | null = null;
  private view: { name: string; yaml: string; current: () => boolean } | null =
    null;
  private sync() {
    const session = this.session(),
      descriptor = canonicalJson(this.spec()),
      baseline = canonicalJson(session.read(this.spec()));
    if (
      this.owner?.session !== session ||
      this.owner.descriptor !== descriptor
    ) {
      this.picked = null;
      this.draft = null;
      this.view = null;
    } else if (this.owner.baseline !== baseline) this.draft = null;
    this.owner = { session, descriptor, baseline };
  }
  private current(): boolean {
    return (
      !this.lifetime.destroyed &&
      this.session().isCurrent() &&
      !!this.session().resolve(this.spec())
    );
  }
  private editable(): boolean {
    const entry = this.session().resolve(this.spec());
    return (
      this.current() &&
      !this.readOnly() &&
      !!entry &&
      !this.session().readOnly(entry) &&
      !this.session().controller.readOnlyReason()
    );
  }
  value(): string {
    const value = this.session().read(this.spec());
    return value.mode === "fixed" && typeof value.value === "string"
      ? value.value
      : "";
  }
  modes(): ResourceMode[] {
    return resourceModes(this.spec());
  }
  mode(): ResourceMode {
    this.sync();
    return (
      this.picked ??
      firstMode(
        this.modes(),
        this.value(),
        this.choices().some((choice) => String(choice.value) === this.value()),
      )
    );
  }
  text(): string {
    this.sync();
    return this.draft?.text ?? this.value();
  }
  hasDraft(): boolean {
    this.sync();
    return this.draft !== null && this.draft.text !== this.value();
  }
  problem(): string {
    this.sync();
    return this.draft
      ? resourceProblem(this.draft.mode, this.draft.text.trim())
      : "";
  }
  description(): string {
    return [
      this.describedBy(),
      this.problem() ? this.controlId() + "-problem" : "",
    ]
      .filter(Boolean)
      .join(" ");
  }
  options(): SelectOption[] {
    const options = this.choices().map((choice) => ({
      value: String(choice.value),
      label: choice.label,
      description: choice.disabled ?? choice.description,
      group: choice.group,
      disabled: !!choice.disabled,
    }));
    const value = this.value();
    if (value && !options.some((option) => option.value === value))
      options.unshift({
        value,
        label: value,
        description: "Not in the available list. The imported value is kept.",
        group: undefined,
        disabled: false,
      });
    return options;
  }
  placeholder(): string {
    return this.mode() === "url"
      ? "https://"
      : this.mode() === "id"
        ? "Item ID"
        : (this.spec().placeholder ?? "name@1.0.0");
  }
  choose(mode: ResourceMode): void {
    if (this.editable() && this.modes().includes(mode)) {
      this.sync();
      this.picked = mode;
    }
  }
  modeKey(event: KeyboardEvent): void {
    if (
      ![
        "ArrowLeft",
        "ArrowRight",
        "ArrowUp",
        "ArrowDown",
        "Home",
        "End",
      ].includes(event.key) ||
      !this.editable()
    )
      return;
    event.preventDefault();
    event.stopPropagation();
    const modes = this.modes(),
      at = modes.indexOf(this.mode());
    const index =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? modes.length - 1
          : (at +
              (["ArrowLeft", "ArrowUp"].includes(event.key)
                ? modes.length - 1
                : 1)) %
            modes.length;
    this.choose(modes[index]);
    this.element.nativeElement
      .querySelectorAll<HTMLElement>(".param-resource-modes [role=radio]")
      [index]?.focus();
  }
  edit(event: Event): void {
    if (!this.editable()) return;
    this.sync();
    this.picked = this.mode();
    this.draft = {
      text: (event.target as HTMLInputElement).value,
      mode: this.picked,
    };
  }
  typed(): void {
    this.sync();
    if (!this.draft || !this.editable() || this.problem()) return;
    const text = this.draft.text.trim();
    if (text === this.value()) {
      this.draft = null;
      return;
    }
    if (!text) {
      this.session().clear(this.spec());
      if (this.session().read(this.spec()).mode === "absent") this.draft = null;
    } else if (
      this.session().write(this.spec(), { mode: "fixed", value: text })
    )
      this.draft = null;
  }
  write(value: string): void {
    if (!this.editable()) return;
    const choice = this.choices().find(
      (choice) => String(choice.value) === value,
    );
    if (
      choice &&
      !choice.disabled &&
      this.session().write(this.spec(), { mode: "fixed", value })
    )
      this.draft = null;
  }
  document(): Json | null {
    if (
      !this.current() ||
      !this.value() ||
      this.spec().path[0] !== "uses" ||
      (this.session().host.profile && !this.session().host.can("catalog.read"))
    )
      return null;
    const session = this.session(),
      context = session.controller.kindContext();
    return session.controller.step(session.target)?.kind === "decisionTable"
      ? context.tableContract(this.value())
      : context.actionContract(this.value());
  }
  open(): void {
    const document = this.document();
    if (!document) return;
    const session = this.session(),
      spec = this.spec(),
      descriptor = canonicalJson(spec),
      api = session.host.api,
      owns = session.owns(spec),
      original = canonicalJson(document);
    this.view = {
      name: this.value(),
      yaml: stringify(document),
      current: () =>
        this.current() &&
        this.session() === session &&
        session.host.api === api &&
        canonicalJson(this.spec()) === descriptor &&
        owns() &&
        canonicalJson(this.document()) === original,
    };
  }
  viewer() {
    if (this.view && !this.view.current()) this.view = null;
    return this.view;
  }
  close(): void {
    this.view = null;
  }
  async copy(): Promise<void> {
    const view = this.viewer();
    if (!view) return;
    try {
      await navigator.clipboard.writeText(view.yaml);
      if (this.view === view && view.current())
        this.session().announce(`Copied ${view.name} as YAML.`);
    } catch {
      if (this.view === view && view.current())
        this.session().announce("Studio couldn't reach the clipboard.");
    }
  }
}
