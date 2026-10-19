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
// The Mapped editor of a field. A lone reference at rest is a pill (a click
// opens the data picker; typing on it opens the formula editor); any other
// formula or template is edited as text in the formula editor, where
// references are pills too. Text that parses is written as typed; a literal
// waits for blur; text that doesn't parse stays with "Not applied" while the
// workflow keeps the last value that applied; on blur the field shows the
// canonical text. Trees the editor can't show stay read-only.
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  type OnInit,
  afterNextRender,
  computed,
  inject,
  input,
  output,
  signal,
  untracked,
  viewChild,
} from "@angular/core";
import { canonicalJson } from "../../../forms/core/json";
import { ReferenceCombobox } from "../../../forms/ui/reference-combobox";
import { Icon } from "../../../icon";
import { completeAt } from "../../formula/complete";
// The editor's class is used only inside @defer, so CodeMirror stays in its own chunk.
import { FormulaEditor } from "../../formula/codemirror/formula-editor";
import type { EditorRequest } from "../../formula/codemirror/formula-editor";
import type { PillLook } from "../../formula/codemirror/decorations";
import {
  fieldText,
  readFieldText,
  type FieldText,
} from "../../formula/field-text";
import { isReference } from "../../formula/grammar";
import { printRef } from "../../formula/printer";
import { referenceSpans, type ReferenceSpan } from "../../formula/spans";
import { tokenize } from "../../formula/tokens";
import { insertPlaceholder, printTemplate } from "../../formula/template";
import { UNAVAILABLE, referencesOf } from "../../formula/preview";
import type { PreviewResult } from "../registry";
import type { ParamValue } from "./value-io";
import { FormulaResult } from "./formula-result";
import { isMappedText } from "../pending-entries";
import type { ParamSpec } from "../registry";
import type { FormSession } from "./form-session";

/** "Input › customerId" → its words for a screen reader. */
const words = (breadcrumb: string) =>
  breadcrumb.replace(/›/g, " ").replace(/\s+/g, " ").trim();
const ROOT_LABELS: Record<string, string> = {
  input: "Input",
  item: "Current item",
  index: "Item position",
  loops: "Loop",
};
/** A breadcrumb for a pointer no suggestion describes: "Input › a › b". */
function fallbackBreadcrumb(pointer: string): string {
  const segments = pointer
    .slice(1)
    .split("/")
    .map((segment) => segment.replace(/~1/g, "/").replace(/~0/g, "~"));
  if (segments[0] === "steps")
    return [
      segments[1],
      ...segments.slice(segments[2] === "output" ? 3 : 2),
    ].join(" › ");
  return [
    ROOT_LABELS[segments[0]] ?? printRef(pointer),
    ...segments.slice(1),
  ].join(" › ");
}

@Component({
  selector: "weave-ref-pill",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  template: `<span class="ref-pill" [attr.title]="title() || pointer()">
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
  title = input("");
  readonly parts = computed(() => this.breadcrumb().split(" › "));
}

@Component({
  selector: "weave-formula-field",
  standalone: true,
  host: {
    "(focusout)": "hostFocusOut($event)",
  },
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [ReferenceCombobox, RefPill, FormulaEditor, FormulaResult, Icon],
  template: `@switch (view()) {
    @case ("pill") {
      <button
        type="button"
        class="ref-pill-button"
        [id]="controlId()"
        [disabled]="!!readOnly()"
        [attr.aria-label]="'reference ' + words(breadcrumbOf(loneRef()))"
        [attr.aria-describedby]="describedBy()"
        (click)="beginPick()"
        (dblclick)="beginPick()"
        (keydown)="pillKey($event)"
      >
        <weave-ref-pill
          [pointer]="loneRef()"
          [breadcrumb]="breadcrumbOf(loneRef())"
          [icon]="iconOf(loneRef())"
          [title]="printRef(loneRef())"
        />
      </button>
    }
    @case ("picker") {
      <div
        class="param-picker"
        (keydown.enter)="commitTyped()"
        (focusout)="leavePicker($event)"
      >
        <weave-reference-combobox
          [inputId]="controlId()"
          [ariaLabel]="spec().label"
          [value]="replacing ? '' : loneRef()"
          [options]="session().scope(spec())"
          [target]="session().expected(spec())"
          [focusOnMount]="picking()"
          [disabled]="!!readOnly()"
          [describedByIds]="describedBy()"
          (picked)="pick($event.ref)"
          (valueChange)="typedRef = $event"
        />
      </div>
    }
    @case ("opaque") {
      <code
        class="param-formula"
        [id]="controlId()"
        tabindex="0"
        [attr.aria-describedby]="describedBy()"
        >{{ jsonText() }}</code
      >
      <p class="param-deferred">
        This formula uses parts the editor can't show. Edit it with Edit as YAML
        in the More menu.
      </p>
    }
    @default {
      <div class="formula-control">
        @defer (on immediate) {
          @if (templateMode()) {
            <weave-formula-editor
              #editor
              mode="template"
              [multiline]="inDialog() || spec().type === 'multiline'"
              [controlId]="controlId()"
              [labelledBy]="controlId() + '-label'"
              [describedBy]="describedBy()"
              [required]="!!spec().required"
              [readOnly]="!!readOnly()"
              [invalid]="!!heldText()"
              [text]="shownText()"
              [revision]="edits()"
              [error]="editorError()"
              [pills]="look"
              [completions]="completions"
              [request]="request()"
              (textChange)="typed($event)"
              (focused)="focusIn()"
              (escaped)="escape($event)"
              (undo)="session().host.undo()"
              (redo)="session().host.redo()"
              (pick)="pickFor($event)"
              [dropRefs]="inDialog()"
              (dropped)="dropText($event)"
            />
          } @else {
            <weave-formula-editor
              #editor
              mode="formula"
              [multiline]="inDialog()"
              [controlId]="controlId()"
              [labelledBy]="controlId() + '-label'"
              [describedBy]="describedBy()"
              [required]="!!spec().required"
              [readOnly]="!!readOnly()"
              [invalid]="!!heldText()"
              [text]="shownText()"
              [revision]="edits()"
              [error]="editorError()"
              [pills]="look"
              [completions]="completions"
              [request]="request()"
              (textChange)="typed($event)"
              (focused)="focusIn()"
              (escaped)="escape($event)"
              (undo)="session().host.undo()"
              (redo)="session().host.redo()"
              (pick)="pickFor($event)"
              [dropRefs]="inDialog()"
              (dropped)="dropText($event)"
            />
          }
        } @placeholder {
          <code
            class="param-input param-formula-loading"
            [id]="controlId()"
            tabindex="0"
            >{{ shownText() }}</code
          >
        }
      </div>
      @if (focusedIn() || inDialog()) {
        <weave-formula-result
          [label]="spec().label"
          [preview]="resultPreview()"
          [source]="resultSource()"
          [tip]="resultTip()"
        />
      }
      <!-- After the Result panel, so Tab from the formula reaches Show all
           first; drawn at the box's right end. -->
      @if (!readOnly() && !inDialog() && expandable()) {
        <button
          type="button"
          class="icon-button formula-expand"
          aria-label="Edit formula"
          title="Edit formula"
          (click)="expand.emit()"
        >
          <weave-icon name="expand" [size]="16" />
        </button>
      }
    }
  }`,
})
export class FormulaField implements OnInit {
  session = input.required<FormSession>();
  spec = input.required<ParamSpec>();
  controlId = input.required<string>();
  describedBy = input("");
  readOnly = input<string | null>(null);
  /** Escape reverts an unapplied formula here; the Edit formula dialog closes first instead. */
  escapeRestores = input(true);
  /** Inside Edit formula: multiline, takes drops itself, Result always shown, no ⤢. */
  inDialog = input(false);
  /** Offers ⤢ Edit formula (the field around it opens the dialog). */
  expandable = input(true);
  readonly expand = output<void>();
  /** The person asked to choose data (Use data…, a pill pressed). */
  readonly picking = signal(false);
  /** Text being typed, while it is the person's and not yet shown in canonical form. */
  readonly editingText = signal<string | null>(null);
  /** How many texts the editor sent: what the field shows next replaces the typed text even when it reads the same as before. */
  readonly edits = signal(0);
  readonly request = signal<EditorRequest | null>(null);
  /** The editor has focus: the Result panel is open. */
  readonly focusedIn = signal(false);
  readonly editor = viewChild<FormulaEditor>("editor");
  readonly words = words;
  readonly printRef = printRef;
  /** A pill of the editor being replaced through the data picker. */
  replacing: { from: number; to: number } | null = null;
  private seq = 0;
  private readonly lifetime = inject(DestroyRef);
  private readonly injector = inject(Injector);
  // Optional: unit tests create the field outside a view.
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef, {
    optional: true,
  });
  private pendingRef = "";
  private localOwner: {
    session: FormSession;
    descriptor: string;
    choices: ParamSpec["choices"];
    readOnly: ParamSpec["readOnly"];
    showWhen: ParamSpec["showWhen"];
  } | null = null;
  private pickerOwner: {
    session: FormSession;
    descriptor: string;
    owns: () => boolean;
  } | null = null;
  private editingOwner: {
    session: FormSession;
    descriptor: string;
    owns: () => boolean;
    stored: string;
    mode: "formula" | "template";
  } | null = null;

  /**
   * A request left for this field when it switched to Mapped (`{{` typed in
   * a Fixed field): the field is new then, so its first check takes it.
   */
  ngOnInit() {
    // Removed while it had focus (step details closed, another step opened).
    this.lifetime.onDestroy(() => {
      if (this.focusedIn()) this.session().leaveFormula(this.spec(), false);
    });
    const request = this.session().takeEditorRequest(this.spec());
    if (request) this.request.set({ ...request, seq: ++this.seq });
  }

  get typedRef(): string {
    return this.pendingRef;
  }
  set typedRef(value: string) {
    this.syncIdentity();
    this.pendingRef = value;
    this.pickerOwner = this.captureOwner();
  }
  private captureOwner() {
    const session = this.session();
    const spec = this.spec();
    return {
      session,
      descriptor: canonicalJson(spec),
      owns: session.owns(spec),
    };
  }
  private syncIdentity() {
    const session = this.session();
    const spec = this.spec();
    const descriptor = canonicalJson(spec);
    const owner = this.localOwner;
    if (
      owner?.session === session &&
      owner.descriptor === descriptor &&
      (typeof spec.choices !== "function" || owner.choices === spec.choices) &&
      owner.readOnly === spec.readOnly &&
      owner.showWhen === spec.showWhen
    )
      return;
    this.localOwner = {
      session,
      descriptor,
      choices: spec.choices,
      readOnly: spec.readOnly,
      showWhen: spec.showWhen,
    };
    this.editingOwner = null;
    this.pickerOwner = null;
    this.pendingRef = "";
    this.replacing = null;
    untracked(() => {
      this.editingText.set(null);
      this.picking.set(false);
    });
  }

  /** Text that doesn't parse, kept with its message while the workflow keeps the last value. */
  heldText(): {
    text: string;
    message: string;
    kind: "template" | "formula";
  } | null {
    const held = this.session().held(this.spec());
    return held && isMappedText(held.kind)
      ? {
          text: held.text,
          message: held.message,
          kind: held.kind as "template" | "formula",
        }
      : null;
  }
  templateError(): string {
    this.syncIdentity();
    return this.heldText()?.message ?? "";
  }
  private stored() {
    return this.session().read(this.spec());
  }
  /** Templates are for text fields on platforms with text.concat, when the value reads as one. */
  templateMode(): boolean {
    if (this.editingOwner) return this.editingOwner.mode === "template";
    const held = this.heldText();
    if (held) return held.kind === "template";
    if (!this.session().templates(this.spec())) return false;
    const value = this.stored();
    return value.mode !== "mapped" || printTemplate(value.expression) !== null;
  }
  private fieldText(): FieldText {
    const spec = this.spec();
    return {
      mode: this.templateMode() ? "template" : "formula",
      options: this.session().formulaOptions(spec),
      formulaOnly:
        spec.mapping === "mapped" ||
        spec.type === "formula" ||
        spec.type === "fileRef",
    };
  }
  loneRef(): string {
    const value = this.stored();
    if (value.mode !== "mapped") return "";
    const expression = value.expression as Record<string, unknown>;
    return Object.keys(expression).length === 1 &&
      typeof expression["ref"] === "string"
      ? expression["ref"]
      : "";
  }
  private reconcile() {
    this.syncIdentity();
    if (
      this.editingOwner &&
      this.session().isCurrent() &&
      this.editingOwner.stored !== canonicalJson(this.stored())
    ) {
      untracked(() => this.editingText.set(null));
      this.beginEdit();
    }
  }
  view(): "pill" | "picker" | "editor" | "opaque" {
    this.reconcile();
    if (this.picking()) return "picker";
    if (this.heldText() || this.editingText() !== null || this.editingOwner)
      return "editor";
    if (this.loneRef()) return "pill";
    return this.storedText() === null ? "opaque" : "editor";
  }
  /**
   * The stored value as the editor's text. Empty Fixed text switched to
   * Mapped is an empty formula, not `""`.
   */
  private storedText(): string | null {
    const value = this.stored();
    if (value.mode === "fixed" && value.value === "") return "";
    return fieldText(value, this.templateMode() ? "template" : "formula");
  }
  shownText(): string {
    this.reconcile();
    return (
      this.heldText()?.text ?? this.editingText() ?? this.storedText() ?? ""
    );
  }
  jsonText(): string {
    const value = this.stored();
    return value.mode === "mapped" ? JSON.stringify(value.expression) : "";
  }
  editorError(): { from: number; to: number } | null {
    const held = this.heldText();
    if (!held) return null;
    const read = readFieldText(held.text, this.fieldText());
    return read.kind === "error"
      ? { from: read.error.from, to: read.error.to }
      : null;
  }

  private canEdit(): boolean {
    return (
      !this.lifetime.destroyed &&
      !this.readOnly() &&
      !this.session().controller.readOnlyReason() &&
      this.session().isCurrent()
    );
  }
  beginPick() {
    this.syncIdentity();
    if (!this.canEdit()) return;
    this.pickerOwner = this.captureOwner();
    this.picking.set(true);
  }
  beginEdit() {
    this.syncIdentity();
    this.editingOwner = {
      ...this.captureOwner(),
      stored: canonicalJson(this.stored()),
      mode:
        this.editingOwner?.mode ??
        (this.templateMode() ? "template" : "formula"),
    };
  }
  private ownsEdit(): boolean {
    const owner = this.editingOwner;
    return (
      !!owner &&
      owner.session === this.session() &&
      owner.descriptor === canonicalJson(this.spec()) &&
      owner.owns() &&
      this.canEdit()
    );
  }
  /** Puts focus in the editor, at the end or at a caret, optionally with the suggestions open. */
  focusEditor(caret: number | "end" = "end", complete = false) {
    this.request.set({ seq: ++this.seq, caret, complete });
  }

  typed(text: string) {
    this.edits.update((count) => count + 1);
    this.reconcile();
    if (!this.canEdit()) return;
    if (!this.ownsEdit()) this.beginEdit();
    if (!this.ownsEdit()) return;
    this.editingText.set(text);
    const field = this.fieldText();
    const read = readFieldText(text, field);
    const session = this.session();
    if (read.kind === "error")
      return session.hold(
        this.spec(),
        field.mode === "template" ? "template" : "formula",
        text,
        read.error.message,
      );
    session.release(this.spec());
    // A literal waits for blur so the field never turns Fixed under the caret.
    if (read.kind === "empty" || read.literal) return;
    if (session.write(this.spec(), read.value)) this.beginEdit();
  }
  leave() {
    this.syncIdentity();
    if (this.heldText()) return;
    const text = this.editingText();
    if (text !== null && this.ownsEdit()) {
      const session = this.session();
      const spec = this.spec();
      const read = readFieldText(text, this.fieldText());
      if (read.kind === "empty") {
        session.clear(spec);
        void session.setMode(spec, "mapped");
      } else if (read.kind === "value") session.write(spec, read.value);
    }
    this.editingOwner = null;
    this.editingText.set(null);
  }
  focusIn() {
    const opening = !this.focusedIn();
    this.focusedIn.set(true);
    this.session().focusFormula(this.spec());
    this.beginEdit();
    // The Result panel opens under the field: scroll the form so it shows.
    if (opening && !this.inDialog())
      afterNextRender(
        () =>
          this.element?.nativeElement
            .querySelector(".formula-result")
            ?.scrollIntoView({ block: "nearest" }),
        { injector: this.injector },
      );
  }
  /** The workflow already holds this value. */
  private holds(value: ParamValue): boolean {
    return canonicalJson(value) === canonicalJson(this.stored());
  }
  /**
   * Focus left the editor or the Result panel: moving into the panel (Show
   * all) keeps the field open; anywhere else leaves it.
   */
  hostFocusOut(event: FocusEvent) {
    if (!this.focusedIn()) return;
    const next = event.relatedTarget as Element | null;
    const host = event.currentTarget as HTMLElement;
    if (next && host.contains(next) && next.closest("weave-formula-result"))
      return;
    this.focusOut();
  }
  focusOut() {
    this.focusedIn.set(false);
    // In Edit formula, focus moves to the Input column to drag a field into
    // an emptied formula: the empty text waits for Done.
    if (!(this.inDialog() && this.editingText() === "")) this.leave();
    this.session().leaveFormula(this.spec());
  }
  /** What the text on screen resolves to: its parse error, or the preview of its value. */
  resultPreview(): PreviewResult | null {
    const read = readFieldText(this.shownText(), this.fieldText());
    if (read.kind === "error" && this.inDialog())
      return {
        ok: false,
        error: { code: read.error.code, message: read.error.message },
      };
    // The line under the field already says why the text doesn't apply.
    if (read.kind === "error")
      return {
        ok: false,
        error: {
          code: UNAVAILABLE,
          message: "No result until the formula applies.",
        },
      };
    if (read.kind === "empty") return null;
    return this.session().previewOf(this.spec(), read.value);
  }
  resultSource(): string {
    const read = readFieldText(this.shownText(), this.fieldText());
    return read.kind === "value"
      ? this.session().previewSource(this.spec(), read.value)
      : "";
  }
  /** The tip to drag a field shows while the text reads no data (even text that doesn't parse yet). */
  resultTip(): boolean {
    const text = this.shownText();
    const read = readFieldText(text, this.fieldText());
    if (read.kind === "value")
      return (
        read.value.mode !== "mapped" ||
        !referencesOf(read.value.expression).length
      );
    return !referenceSpans(
      tokenize(text, this.templateMode() ? "template" : "formula"),
    ).length;
  }

  /** A dropped Input field: its path at the drop point. */
  dropText(drop: { ref: string; pos: number }) {
    this.reconcile();
    if (!this.canEdit()) return;
    const path = printRef(drop.ref);
    const text = this.shownText();
    const pos = Math.max(0, Math.min(drop.pos, text.length));
    let insert = path;
    let from = pos;
    let to = pos;
    if (this.templateMode()) {
      const inside = tokenize(text, "template")
        .filter((token) => token.from < pos)
        .reduce(
          (open, token) =>
            token.type === "open"
              ? true
              : token.type === "close"
                ? false
                : open,
          false,
        );
      if (!inside) {
        // Outside a placeholder: `{{ path }}`, keeping backslashes before it as text.
        const placed = insertPlaceholder(text, pos, path);
        this.beginEdit();
        this.typed(placed.text);
        this.focusEditor(placed.caret);
        return;
      }
    } else {
      const current = readFieldText(text, this.fieldText());
      if (
        current.kind === "empty" ||
        (current.kind === "value" &&
          current.value.mode === "mapped" &&
          isReference(current.value.expression))
      ) {
        from = 0;
        to = text.length;
      }
    }
    this.beginEdit();
    this.typed(text.slice(0, from) + insert + text.slice(to));
    this.focusEditor(from + insert.length);
  }

  /** Text on screen that the workflow doesn't hold yet: held, or a literal or emptiness waiting for blur. */
  private unapplied(): boolean {
    if (this.heldText()) return true;
    const text = this.editingText();
    if (text === null) return false;
    const read = readFieldText(text, this.fieldText());
    return (
      (read.kind === "empty" ||
        (read.kind === "value" && read.literal && !this.holds(read.value))) &&
      text !== (this.storedText() ?? "")
    );
  }
  /** A drop on the editor in the form: the path at the text position under the pointer. */
  dropAt(ref: string, x: number, y: number) {
    const editor = this.editor();
    this.dropText({
      ref,
      pos: editor ? editor.posAt(x, y) : this.shownText().length,
    });
  }

  escape(signal: { event: KeyboardEvent; handled: boolean }) {
    if (!this.unapplied() || !this.escapeRestores()) return;
    signal.handled = true;
    const session = this.session();
    if (session.read(this.spec()).mode !== "mapped")
      void session.setMode(this.spec(), "mapped");
    session.release(this.spec());
    this.editingText.set(null);
    this.beginEdit();
    this.focusEditor("end");
    session.host.refreshView();
  }

  /** Typing a character on a pill opens the editor with the path and that character. */
  pillKey(event: KeyboardEvent) {
    if (this.readOnly()) return;
    if (event.key === "Backspace" || event.key === "Delete") {
      event.preventDefault();
      this.session().clear(this.spec());
      return;
    }
    if (
      event.key.length !== 1 ||
      event.key === " " ||
      event.ctrlKey ||
      event.metaKey ||
      event.altKey
    )
      return;
    event.preventDefault();
    const text = printRef(this.loneRef()) + event.key;
    this.beginEdit();
    this.typed(text);
    this.focusEditor("end", event.key === ".");
  }

  breadcrumbOf(ref: string): string {
    const entry = this.session()
      .formulaScope(this.spec())
      .entries.find((item) => item.ref === ref);
    return (entry?.breadcrumb ?? fallbackBreadcrumb(ref)).replace(
      /^Workflow input(?= ›|$)/,
      "Input",
    );
  }
  iconOf(ref: string): string {
    const entry = this.session()
      .formulaScope(this.spec())
      .entries.find((item) => item.ref === ref);
    return entry?.source === "step" ? (entry.stepKind ?? "action") : "source";
  }
  readonly look = (pointer: string): PillLook => {
    const breadcrumb = this.breadcrumbOf(pointer);
    return {
      parts: breadcrumb.split(" › "),
      icon: this.iconOf(pointer),
      title: printRef(pointer),
      name: `reference ${words(breadcrumb)}`,
    };
  };
  readonly completions = (text: string, caret: number) =>
    completeAt(
      text,
      caret,
      this.templateMode() ? "template" : "formula",
      this.session().completionScope(this.spec()),
    );

  /** Double-click on a pill in the editor: choose other data for it. */
  pickFor(span: ReferenceSpan) {
    this.replacing = { from: span.from, to: span.to };
    this.beginPick();
  }
  pick(ref: string) {
    this.syncIdentity();
    const owner = this.pickerOwner;
    if (
      !this.canEdit() ||
      !owner ||
      owner.session !== this.session() ||
      owner.descriptor !== canonicalJson(this.spec()) ||
      !owner.owns() ||
      !/^\/(input|steps|item|index|loops)(\/|$)/.test(ref)
    )
      return;
    const focused = document.activeElement?.id === this.controlId();
    this.pendingRef = "";
    this.pickerOwner = null;
    this.picking.set(false);
    const replacing = this.replacing;
    this.replacing = null;
    if (replacing) {
      const text = this.shownText();
      const path = printRef(ref);
      this.beginEdit();
      this.typed(
        text.slice(0, replacing.from) + path + text.slice(replacing.to),
      );
      this.focusEditor(replacing.from + path.length);
      return;
    }
    this.session().write(this.spec(), { mode: "mapped", expression: { ref } });
    if (focused) this.focusControlAfterRender();
  }
  commitTyped() {
    this.syncIdentity();
    const ref = this.typedRef.trim();
    if (ref && ref !== this.loneRef()) this.pick(ref);
  }
  leavePicker(event: FocusEvent) {
    const next = event.relatedTarget as Node | null;
    if (next && (event.currentTarget as HTMLElement).contains(next)) return;
    if (!next && !document.hasFocus()) return;
    this.commitTyped();
    const typed = this.typedRef.trim();
    if (typed && typed !== this.loneRef()) return;
    this.pendingRef = "";
    this.replacing = null;
    this.picking.set(false);
  }
  private focusControlAfterRender() {
    const id = this.controlId();
    afterNextRender(
      () => {
        if (this.lifetime.destroyed) return;
        const active = document.activeElement;
        if (active && active !== document.body && active.id !== id) return;
        document.getElementById(id)?.focus();
      },
      { injector: this.injector },
    );
  }
}
