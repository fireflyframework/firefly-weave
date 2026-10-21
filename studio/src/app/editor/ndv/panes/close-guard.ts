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
// The question before an unsaved output edit goes: closing step details,
// moving to another step, switching panes or leaving the editor. Save is
// the first answer and starts focused; Escape keeps editing.
import {
  type AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  inject,
  output,
} from "@angular/core";

export type GuardAnswer = "save" | "discard" | "keep";
let sequence = 0;

@Component({
  selector: "weave-close-guard",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  // The host is the backdrop, so step details leave it usable (keepUsable).
  host: { class: "modal-backdrop", "(mousedown)": "hold($event)" },
  template: `<section
    class="modal-panel close-guard"
    role="alertdialog"
    aria-modal="true"
    tabindex="-1"
    [attr.aria-labelledby]="titleId"
    [attr.aria-describedby]="messageId"
    (keydown)="key($event)"
  >
    <header class="modal-header">
      <h2 [id]="titleId">Save the output changes before closing?</h2>
    </header>
    <p class="dialog-message" [id]="messageId">
      Your edit of this step's test output isn't saved yet.
    </p>
    <div class="dialog-actions">
      <button type="button" (click)="answer.emit('keep')">Keep editing</button>
      <button type="button" class="danger" (click)="answer.emit('discard')">
        Discard
      </button>
      <button
        type="button"
        class="primary"
        data-guard-save
        (click)="answer.emit('save')"
      >
        Save
      </button>
    </div>
  </section>`,
})
export class CloseGuard implements AfterViewInit {
  readonly answer = output<GuardAnswer>();
  readonly titleId = `close-guard-title-${++sequence}`;
  readonly messageId = `close-guard-message-${sequence}`;
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);

  ngAfterViewInit() {
    // After step details settle their own focus for the key that asked.
    setTimeout(() =>
      this.element.nativeElement
        .querySelector<HTMLElement>("[data-guard-save]")
        ?.focus(),
    );
  }
  private buttons(): HTMLElement[] {
    return [...this.element.nativeElement.querySelectorAll("button")];
  }
  /** A click on the backdrop keeps focus in the question. */
  hold(event: MouseEvent) {
    if (event.target === event.currentTarget) event.preventDefault();
  }
  key(event: KeyboardEvent) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      this.answer.emit("keep");
      return;
    }
    if (event.key !== "Tab") return;
    const buttons = this.buttons();
    const at = buttons.indexOf(document.activeElement as HTMLElement);
    const next = event.shiftKey
      ? at <= 0
        ? buttons.length - 1
        : at - 1
      : at === buttons.length - 1
        ? 0
        : at + 1;
    event.preventDefault();
    buttons[next]?.focus();
  }
}
