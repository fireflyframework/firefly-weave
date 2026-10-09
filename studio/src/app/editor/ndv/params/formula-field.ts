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
// The Mapped editor of a field. A reference shows as a pill and is changed
// with the data picker; a text field with templates is edited as text with
// {{ input.field }} placeholders; any other formula shows read-only (it is
// edited as YAML from the More menu).
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
} from "@angular/core";
import { canonicalJson } from "../../../forms/core/json";
import { Icon } from "../../../icon";
import { ReferenceCombobox } from "../../../forms/ui/reference-combobox";
import type { ParamSpec } from "../registry";
import type { FormSession } from "./form-session";
import {
  partsToText,
  refToPath,
  templateParts,
  textToExpression,
} from "./template-text";

/** "Input › customerId" → its words for a screen reader. */
const words = (breadcrumb: string) =>
  breadcrumb.replace(/›/g, " ").replace(/\s+/g, " ").trim();

@Component({
  selector: "weave-ref-pill",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  template: `<span class="ref-pill" [attr.title]="pointer()">
    <weave-icon [name]="icon()" [size]="16" />
    @for (part of parts(); track $index; let last = $last) {
      <span>{{ part }}</span>
      @if (!last) {
        <span class="ref-sep" aria-hidden="true">›</span>
      }
    }
  </span>`,
})
export class RefPill {
  pointer = input.required<string>();
  breadcrumb = input.required<string>();
  icon = input("source");
  readonly parts = computed(() => this.breadcrumb().split(" › "));
}

@Component({
  selector: "weave-formula-field",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [ReferenceCombobox, RefPill],
  template: `@switch (view()) {
    @case ("template") {
      @if (spec().type === "multiline") {
        <textarea
          class="param-input param-multiline param-template"
          rows="3"
          [id]="controlId()"
          [attr.aria-label]="spec().label"
          [attr.aria-required]="spec().required || null"
          [attr.aria-describedby]="describedBy()"
          [attr.aria-invalid]="templateError() ? 'true' : null"
          [readOnly]="!!readOnly()"
          [value]="templateText()"
          (focus)="beginEdit()"
          (input)="typed($event)"
          (blur)="leave()"
        ></textarea>
      } @else {
        <input
          class="param-input param-template"
          [id]="controlId()"
          [attr.aria-label]="spec().label"
          [attr.aria-required]="spec().required || null"
          [attr.aria-describedby]="describedBy()"
          [attr.aria-invalid]="templateError() ? 'true' : null"
          [readOnly]="!!readOnly()"
          [value]="templateText()"
          (focus)="beginEdit()"
          (input)="typed($event)"
          (blur)="leave()"
        />
      }
      @if (templateError()) {
        <span class="param-template-error" role="alert">{{
          templateError()
        }}</span>
      }
    }
    @case ("formula") {
      <code
        class="param-formula"
        [id]="controlId()"
        tabindex="0"
        [attr.aria-describedby]="describedBy()"
        >{{ formulaText() }}</code
      >
    }
    @case ("pill") {
      <button
        type="button"
        class="ref-pill-button"
        [id]="controlId()"
        [disabled]="!!readOnly()"
        [attr.aria-label]="'reference ' + words(breadcrumbOf(currentRef()))"
        [attr.aria-describedby]="describedBy()"
        (click)="picking.set(true)"
        (dblclick)="picking.set(true)"
        (keydown)="pillKey($event)"
      >
        <weave-ref-pill
          [pointer]="currentRef()"
          [breadcrumb]="breadcrumbOf(currentRef())"
          [icon]="iconOf(currentRef())"
        />
      </button>
    }
    @default {
      <div
        class="param-picker"
        (keydown.enter)="commitTyped()"
        (focusout)="commitTyped()"
      >
        <weave-reference-combobox
          [inputId]="controlId()"
          [ariaLabel]="spec().label"
          [value]="currentRef()"
          [options]="session().scope(spec())"
          [target]="session().expected(spec())"
          [focusOnMount]="true"
          [disabled]="!!readOnly()"
          [describedByIds]="describedBy()"
          (picked)="pick($event.ref)"
          (valueChange)="typedRef = $event"
        />
      </div>
    }
  }`,
})
export class FormulaField {
  session = input.required<FormSession>();
  spec = input.required<ParamSpec>();
  controlId = input.required<string>();
  describedBy = input("");
  readOnly = input<string | null>(null);
  readonly picking = signal(false);
  readonly editingText = signal<string | null>(null);
  readonly templateError = signal("");
  readonly words = words;
  typedRef = "";
  private editingOwner: {
    session: FormSession;
    descriptor: string;
    owns: () => boolean;
  } | null = null;
  beginEdit() {
    const session = this.session();
    const spec = this.spec();
    this.editingOwner = {
      session,
      descriptor: canonicalJson(spec),
      owns: session.owns(spec),
    };
  }
  private ownsEdit(): boolean {
    const owner = this.editingOwner;
    return (
      !!owner &&
      owner.session === this.session() &&
      owner.descriptor === canonicalJson(this.spec()) &&
      owner.owns() &&
      !this.readOnly()
    );
  }
  leave() {
    const text = this.editingText();
    if (text !== null) {
      const parsed = textToExpression(text);
      if (!parsed.ok) return;
      if (parsed.value.mode === "fixed" && this.ownsEdit())
        this.session().write(this.spec(), parsed.value);
    }
    this.editingOwner = null;
    this.editingText.set(null);
    this.templateError.set("");
  }

  private stored() {
    return this.session().read(this.spec());
  }
  private parts() {
    const value = this.stored();
    if (value.mode === "mapped") return templateParts(value.expression);
    return value.mode === "fixed" && typeof value.value === "string"
      ? [{ text: value.value }]
      : null;
  }
  currentRef(): string {
    const value = this.stored();
    if (value.mode !== "mapped") return "";
    const ref = (value.expression as { ref?: unknown }).ref;
    return typeof ref === "string" ? ref : "";
  }
  /** template: text with placeholders; pill: one reference; picker: choosing data; formula: anything else, read-only. */
  view(): "template" | "pill" | "picker" | "formula" {
    const value = this.stored();
    if (this.picking()) return "picker";
    const parts = this.parts();
    const single = this.currentRef();
    const editable = parts?.every(
      (part) => "text" in part || refToPath(part.ref) !== null,
    );
    if (
      this.session().templates(this.spec()) &&
      (this.editingText() !== null || (!!parts && editable && !single))
    )
      return "template";
    if (value.mode === "mapped" && !single) return "formula";
    if (single && !this.picking()) return "pill";
    return "picker";
  }
  templateText(): string {
    const parts = this.parts();
    return this.editingText() ?? (parts ? partsToText(parts) : "");
  }
  formulaText(): string {
    const value = this.stored();
    return value.mode === "mapped" ? JSON.stringify(value.expression) : "";
  }
  breadcrumbOf(ref: string): string {
    const entry = this.session()
      .scope(this.spec())
      .find((e) => e.ref === ref);
    return (entry?.breadcrumb ?? ref).replace(
      /^Workflow input(?= ›|$)/,
      "Input",
    );
  }
  iconOf(ref: string): string {
    const entry = this.session()
      .scope(this.spec())
      .find((e) => e.ref === ref);
    return entry?.source === "step" ? (entry.stepKind ?? "action") : "source";
  }
  pick(ref: string) {
    if (
      this.readOnly() ||
      !this.session().isCurrent() ||
      !/^\/(input|steps)(\/|$)/.test(ref)
    )
      return;
    this.typedRef = "";
    this.picking.set(false);
    this.session().write(this.spec(), { mode: "mapped", expression: { ref } });
  }
  /** A pointer typed by hand counts once Enter is pressed or focus leaves. */
  commitTyped() {
    const ref = this.typedRef.trim();
    if (ref && ref !== this.currentRef()) this.pick(ref);
  }
  pillKey(event: KeyboardEvent) {
    if (
      !this.readOnly() &&
      (event.key === "Backspace" || event.key === "Delete")
    ) {
      event.preventDefault();
      this.session().clear(this.spec());
    }
  }
  typed(event: Event) {
    if (!this.editingOwner) this.beginEdit();
    if (!this.ownsEdit()) return;
    const text = (event.target as HTMLInputElement).value;
    this.editingText.set(text);
    const parsed = textToExpression(text);
    if (!parsed.ok) return this.templateError.set(parsed.message);
    this.templateError.set("");
    // Literal text waits for blur so typing braces cannot replace the focused editor.
    if (
      parsed.value.mode === "mapped" &&
      this.session().write(this.spec(), parsed.value)
    )
      this.beginEdit();
  }
}
