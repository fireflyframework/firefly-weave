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
// Notes: a note for the people who read the workflow, kept with its canvas,
// and "Show on canvas" to put its first line under the step's name. Typing
// is one undo step per burst, like a field; a note that is only spaces is
// no note.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  signal,
} from "@angular/core";
import type { FormSession } from "../params/form-session";

let sequence = 0;

@Component({
  selector: "weave-notes-section",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  host: { "[attr.data-param]": "'notes'" },
  template: `@let note = this.note();
    @let locked = !!session().controller.readOnlyReason();
    <div class="param settings-notes">
      <div class="param-label-row">
        <label class="param-label" [for]="id">Notes</label>
        <span class="param-spacer"></span>
        <span class="settings-inline-label" [id]="id + '-canvas'"
          >Show on canvas</span
        >
        <button
          type="button"
          role="switch"
          class="param-switch"
          [attr.aria-checked]="!!note.showOnCanvas"
          [attr.aria-labelledby]="id + '-canvas'"
          [disabled]="locked || !note.text.trim()"
          (click)="toggleCanvas()"
        >
          <span class="param-switch-track" aria-hidden="true"></span>
        </button>
      </div>
      <div class="param-control">
        <textarea
          class="param-input param-multiline"
          rows="3"
          maxlength="2000"
          [id]="id"
          [attr.aria-describedby]="id + '-line'"
          [readOnly]="locked"
          placeholder="Add a note for people who read this workflow"
          [value]="text()"
          (input)="typed($event)"
          (blur)="draft.set(null)"
        ></textarea>
      </div>
      <p class="param-line" data-kind="hint" [id]="id + '-line'">
        Notes don't change how the workflow runs.
      </p>
    </div>`,
})
export class NotesSection {
  session = input.required<FormSession>();
  readonly id = `sd-notes-${++sequence}`;
  /**
   * What the person is typing, while it differs from the stored note only
   * by being blank (a note of spaces is removed, the spaces stay on screen).
   */
  readonly draft = signal<{ text: string; stored: string } | null>(null);

  note() {
    const session = this.session();
    return session.controller.note(session.target);
  }
  /** The text on screen: the stored note, or what is being typed. */
  text(): string {
    const stored = this.note().text;
    const draft = this.draft();
    return draft && draft.stored === stored ? draft.text : stored;
  }
  typed(event: Event) {
    const session = this.session();
    const text = (event.target as HTMLTextAreaElement).value;
    const note = this.note();
    if (
      session.isCurrent() &&
      session.controller.setNote(
        session.target,
        text.trim() ? { ...note, text } : null,
      )
    )
      this.draft.set({ text, stored: this.note().text });
  }
  toggleCanvas() {
    const session = this.session();
    const note = this.note();
    if (!session.isCurrent() || !note.text.trim()) return;
    session.controller.setNote(
      session.target,
      { ...note, showOnCanvas: !note.showOnCanvas },
      "$note-canvas",
    );
  }
}
