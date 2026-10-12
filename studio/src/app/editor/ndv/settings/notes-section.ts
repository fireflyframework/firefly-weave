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
// no note. A note the canvas has no room for stays on screen, marked as not
// saved, until it fits.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  signal,
} from "@angular/core";
import { Icon } from "../../../icon";
import { MAX_STEP_NOTE_TEXT } from "../../state/canvas-sidecar";
import type { FormSession } from "../params/form-session";

let sequence = 0;

/** What is on screen while it differs from the stored note, and why. */
interface NoteDraft {
  text: string;
  /** The stored text it was typed over: a change from elsewhere replaces it. */
  stored: string;
  /** Why it isn't saved; none for a blank note, which is simply no note. */
  refused?: string;
}

@Component({
  selector: "weave-notes-section",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  host: { "[attr.data-param]": "'notes'" },
  template: `@let note = this.note();
    @let locked = !!session().controller.readOnlyReason();
    @let refused = this.refused();
    <div class="param settings-notes" [class.has-error]="!!refused">
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
          rows="2"
          [attr.maxlength]="maxLength"
          [id]="id"
          [attr.aria-describedby]="id + '-line'"
          [attr.aria-invalid]="refused ? 'true' : null"
          [attr.aria-errormessage]="refused ? id + '-line' : null"
          [readOnly]="locked"
          placeholder="Add a note for people who read this workflow"
          [value]="text()"
          (input)="typed($event)"
          (blur)="leave()"
        ></textarea>
      </div>
      @if (refused) {
        <p
          class="param-line"
          data-kind="error"
          role="alert"
          [id]="id + '-line'"
        >
          <weave-icon name="warning" [size]="16" />Not saved. {{ refused }}
        </p>
      } @else {
        <p class="param-line" data-kind="hint" [id]="id + '-line'">
          Notes don't change how the workflow runs.
        </p>
      }
    </div>`,
})
export class NotesSection {
  session = input.required<FormSession>();
  readonly id = `sd-notes-${++sequence}`;
  readonly maxLength = MAX_STEP_NOTE_TEXT;
  readonly draft = signal<NoteDraft | null>(null);

  note() {
    const session = this.session();
    return session.controller.note(session.target);
  }
  /** The draft, while the stored note is still the one it was typed over. */
  private current(): NoteDraft | null {
    const draft = this.draft();
    return draft && draft.stored === this.note().text ? draft : null;
  }
  /** The text on screen: the stored note, or what is being typed. */
  text(): string {
    return this.current()?.text ?? this.note().text;
  }
  /** Why the note on screen isn't saved, if it isn't. */
  refused(): string | null {
    return this.current()?.refused ?? null;
  }
  typed(event: Event) {
    const session = this.session();
    if (!session.isCurrent()) return;
    const text = (event.target as HTMLTextAreaElement).value;
    const note = this.note();
    const refused = session.controller.setNote(
      session.target,
      text.trim() ? { ...note, text } : null,
    );
    this.draft.set({
      text,
      stored: this.note().text,
      ...(refused ? { refused } : {}),
    });
  }
  /** Leaving keeps a refused note on screen; spaces typed into no note go. */
  leave() {
    if (!this.refused()) this.draft.set(null);
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
