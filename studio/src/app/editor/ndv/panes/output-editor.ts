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
// Edit output: a step's pinned output as JSON, in the Output pane. Save
// reads the text, checks it like any pin (a secret is refused) and pins it;
// Cancel, or Escape without a change, leaves the pin as it was. Escape with
// a change goes on to step details, whose close asks first (close-guard).
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  afterNextRender,
  inject,
  input,
  output,
  signal,
  viewChild,
} from "@angular/core";
import { parseJsonText } from "../../../forms/core/json";
import {
  createJsonEditor,
  type JsonEditor,
} from "../../formula/codemirror/json-editor";
import type { FormSession } from "../params/form-session";
import type { Json } from "../registry";

let sequence = 0;

@Component({
  selector: "weave-output-editor",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  template: `<div
      class="output-editor-field"
      [class.is-invalid]="!!error()"
      #field
    ></div>
    @if (error()) {
      <p class="output-editor-error" role="alert" [id]="errorId">
        {{ error() }}
      </p>
    }
    <div class="output-editor-actions">
      <p class="output-editor-note" [id]="noteId">
        Paste data from an earlier run to reuse it.
      </p>
      <div class="output-editor-buttons">
        <button type="button" class="sm" (click)="cancel()">Cancel</button>
        <button
          type="button"
          class="sm primary"
          [disabled]="saving()"
          (click)="save()"
        >
          Save
        </button>
      </div>
    </div>`,
  host: { class: "output-editor" },
})
export class OutputEditor {
  session = input.required<FormSession>();
  /** The text the editor starts with: the pinned output as 2-space JSON, or {}. */
  initial = input.required<string>();
  readonly saved = output<Json>();
  readonly cancelled = output<void>();

  readonly error = signal("");
  readonly saving = signal(false);
  readonly noteId = `output-editor-note-${++sequence}`;
  readonly errorId = `output-editor-error-${sequence}`;
  private readonly text = signal("");
  private editor: JsonEditor | null = null;
  private readonly field = viewChild.required<ElementRef<HTMLElement>>("field");

  constructor() {
    afterNextRender(() => this.mount());
    inject(DestroyRef).onDestroy(() => this.editor?.destroy());
  }

  private mount(): void {
    const initial = this.initial();
    this.text.set(initial);
    this.editor = createJsonEditor(this.field().nativeElement, {
      text: initial,
      label: `Output of ${this.session().target}`,
      describedBy: this.noteId,
      onChange: (text) => {
        this.text.set(text);
        if (this.error()) this.describe("");
      },
      onSave: () => void this.save(),
      onEscape: () => {
        if (this.dirty()) return false;
        this.cancelled.emit();
        return true;
      },
    });
    this.editor.focus();
  }

  /** The text differs from what the editor opened with. */
  dirty(): boolean {
    return this.text() !== this.initial();
  }
  focus(): void {
    this.editor?.focus();
  }
  cancel(): void {
    this.cancelled.emit();
  }
  /** Reads, checks and pins the text; true once it is pinned. */
  async save(): Promise<boolean> {
    if (this.saving()) return false;
    const parsed = parseJsonText(this.text());
    if (!parsed.ok) {
      this.refuse(parsed.message);
      return false;
    }
    const { controller, target } = this.session();
    this.saving.set(true);
    try {
      const refused = await controller.pin(target, parsed.value, "manual");
      if (refused) {
        this.refuse(refused);
        return false;
      }
    } finally {
      this.saving.set(false);
    }
    this.saved.emit(parsed.value);
    return true;
  }
  private refuse(message: string): void {
    this.describe(message);
  }
  /** The error under the editor, which the editor names while it shows. */
  private describe(message: string): void {
    this.error.set(message);
    this.editor?.view.contentDOM.setAttribute(
      "aria-describedby",
      message ? `${this.noteId} ${this.errorId}` : this.noteId,
    );
  }
}
