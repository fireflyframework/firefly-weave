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
// A formula or template field: CodeMirror with Studio's colors, pills and
// keys. Loaded with the first formula field (@defer), so CodeMirror stays out
// of the first page. The text input is what to show; while the person types,
// their text stays until the field sends something else (Undo, blur).
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  afterNextRender,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
} from "@angular/core";
import {
  Annotation,
  Compartment,
  EditorSelection,
  EditorState,
  type Extension,
} from "@codemirror/state";
import { EditorView, placeholder } from "@codemirror/view";
import { REF_MIME } from "../../ndv/params/drag-map";
import { parseDrag } from "../../ndv/params/drop";
import type { ReferenceSpan } from "../spans";
import {
  decorationConfig,
  formulaState,
  formulaTheme,
  refreshDecorations,
  type PillLook,
} from "./decorations";
import { formulaKeys, singleLine } from "./keys";

export interface EditorRequest {
  /** A new number for each request. */
  seq: number;
  caret?: number | "end";
  selectAll?: boolean;
  /** Open the completion list at the caret. */
  complete?: boolean;
}

/** Marks a change the field made, so it isn't sent back as typing. */
const fromField = Annotation.define<boolean>();

@Component({
  selector: "weave-formula-editor",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  template: "",
  host: {
    class: "formula-box",
    "[class.is-template]": "mode() === 'template'",
    "[class.is-multiline]": "multiline()",
    "[class.is-invalid]": "invalid()",
    "[class.is-readonly]": "readOnly()",
    "[attr.data-text]": "docText()",
  },
})
export class FormulaEditor {
  controlId = input.required<string>();
  labelledBy = input.required<string>();
  describedBy = input("");
  /** Fixed once the editor is on screen. */
  mode = input<"formula" | "template">("formula");
  /** Fixed once the editor is on screen. */
  multiline = input(false);
  readOnly = input(false);
  required = input(false);
  invalid = input(false);
  text = input.required<string>();
  error = input<{ from: number; to: number } | null>(null);
  pills = input.required<(pointer: string) => PillLook>();
  request = input<EditorRequest | null>(null);
  /** Take dropped Input fields itself (in a dialog); in the form the field does. */
  dropRefs = input(false);

  readonly textChange = output<string>();
  readonly focused = output<void>();
  readonly blurred = output<void>();
  readonly escaped = output<{ event: KeyboardEvent; handled: boolean }>();
  readonly undo = output<void>();
  readonly redo = output<void>();
  readonly pick = output<ReferenceSpan>();
  readonly dropped = output<{ ref: string; pos: number }>();

  readonly docText = signal("");
  private view: EditorView | null = null;
  private emitted: string | null = null;
  private appliedRequest = -1;
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly attributes = new Compartment();
  private readonly editing = new Compartment();
  /** Extensions that later features add (the completion list). */
  protected extras(): Extension[] {
    return [];
  }

  constructor() {
    afterNextRender(() => this.mount());
    inject(DestroyRef).onDestroy(() => this.view?.destroy());
    effect(() => {
      const text = this.text();
      untracked(() => this.sync(text));
    });
    effect(() => {
      const attributes = this.contentAttributes();
      const readOnly = this.readOnly();
      untracked(() =>
        this.view?.dispatch({
          effects: [
            this.attributes.reconfigure(
              EditorView.contentAttributes.of(attributes),
            ),
            this.editing.reconfigure(EditorState.readOnly.of(readOnly)),
          ],
        }),
      );
    });
    effect(() => {
      this.error();
      this.pills();
      untracked(() =>
        this.view?.dispatch({ effects: refreshDecorations.of(null) }),
      );
    });
    effect(() => {
      const request = this.request();
      if (request) untracked(() => this.apply(request));
    });
  }

  private contentAttributes(): Record<string, string> {
    return {
      role: "textbox",
      id: this.controlId(),
      "aria-labelledby": this.labelledBy(),
      ...(this.describedBy() ? { "aria-describedby": this.describedBy() } : {}),
      ...(this.required() ? { "aria-required": "true" } : {}),
      ...(this.invalid() ? { "aria-invalid": "true" } : {}),
      ...(this.readOnly() ? { "aria-readonly": "true" } : {}),
      ...(this.multiline() ? { "aria-multiline": "true" } : {}),
      spellcheck: "false",
      autocapitalize: "off",
      autocorrect: "off",
    };
  }

  private mount(): void {
    const text = this.text();
    this.docText.set(text);
    this.view = new EditorView({
      parent: this.element.nativeElement,
      state: EditorState.create({
        doc: text,
        extensions: [
          this.attributes.of(
            EditorView.contentAttributes.of(this.contentAttributes()),
          ),
          this.editing.of(EditorState.readOnly.of(this.readOnly())),
          this.multiline() ? EditorView.lineWrapping : singleLine,
          decorationConfig.of({
            mode: this.mode(),
            look: (pointer) => this.pills()(pointer),
            pick: (span) => this.pick.emit(span),
            error: () => this.error(),
          }),
          formulaState,
          formulaTheme,
          formulaKeys({
            multiline: this.multiline(),
            undo: () => this.undo.emit(),
            redo: () => this.redo.emit(),
            escape: (event) => {
              const signal = { event, handled: false };
              this.escaped.emit(signal);
              return signal.handled;
            },
          }),
          ...(this.mode() === "formula"
            ? [placeholder("Type a formula, or drag a field here")]
            : []),
          EditorView.updateListener.of((update) => {
            if (update.docChanged) {
              const next = update.state.doc.toString();
              this.docText.set(next);
              if (
                !update.transactions.some((transaction) =>
                  transaction.annotation(fromField),
                )
              ) {
                this.emitted = next;
                this.textChange.emit(next);
              }
            }
            if (update.focusChanged)
              (update.view.hasFocus ? this.focused : this.blurred).emit();
          }),
          EditorView.domEventHandlers({
            dragover: (event) => {
              if (!event.dataTransfer?.types.includes(REF_MIME)) return false;
              if (this.dropRefs()) event.preventDefault();
              return true;
            },
            drop: (event, view) => {
              if (!event.dataTransfer?.types.includes(REF_MIME)) return false;
              if (!this.dropRefs()) return true;
              event.preventDefault();
              const drag = parseDrag(event.dataTransfer.getData(REF_MIME));
              const pos =
                view.posAtCoords({ x: event.clientX, y: event.clientY }) ??
                view.state.doc.length;
              if (drag) this.dropped.emit({ ref: drag.ref, pos });
              return true;
            },
          }),
          ...this.extras(),
        ],
      }),
    });
    const request = this.request();
    if (request) this.apply(request);
  }

  private sync(text: string): void {
    const view = this.view;
    if (!view) return;
    const current = view.state.doc.toString();
    if (text === current || (view.hasFocus && text === this.emitted)) return;
    view.dispatch({
      changes: { from: 0, to: current.length, insert: text },
      ...(view.hasFocus
        ? { selection: EditorSelection.cursor(text.length) }
        : {}),
      annotations: fromField.of(true),
    });
    this.emitted = null;
  }

  private apply(request: EditorRequest): void {
    const view = this.view;
    if (!view || request.seq === this.appliedRequest) return;
    this.appliedRequest = request.seq;
    view.focus();
    const length = view.state.doc.length;
    if (request.selectAll)
      view.dispatch({ selection: EditorSelection.range(0, length) });
    else if (request.caret !== undefined)
      view.dispatch({
        selection: EditorSelection.cursor(
          request.caret === "end"
            ? length
            : Math.max(0, Math.min(request.caret, length)),
        ),
      });
    if (request.complete) this.openCompletion();
  }

  /** Opens the completion list; the completion list replaces it. */
  protected openCompletion(): void {}

  /** The text position under a point of the screen. */
  posAt(x: number, y: number): number {
    return this.view?.posAtCoords({ x, y }) ?? this.view?.state.doc.length ?? 0;
  }
  /** Replaces a range of the text as typing would, and puts the caret after it. */
  insert(from: number, to: number, text: string): void {
    const view = this.view;
    if (!view) return;
    view.focus();
    view.dispatch({
      changes: { from, to, insert: text },
      selection: EditorSelection.cursor(from + text.length),
      userEvent: "input.drop",
    });
  }
  caret(): number {
    return this.view?.state.selection.main.head ?? 0;
  }
  focus(): void {
    this.view?.focus();
  }
  hasFocus(): boolean {
    return !!this.view?.hasFocus;
  }
}
