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
// The header pill "2 issues": a menu button whose list names each field and
// its problem; choosing one goes to that field (on its tab). It is absent
// when the step has no issues. The list sits in the top layer, so a narrow
// pane never cuts it off.
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  afterNextRender,
  effect,
  inject,
  input,
  output,
  signal,
} from "@angular/core";
import { AnchoredPopover } from "../../forms/ui/anchored-popover";
import { Icon } from "../../icon";
import { issueCount, issueTone, type StepIssue } from "./issues";

let sequence = 0;

@Component({
  selector: "weave-issues-indicator",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, AnchoredPopover],
  host: {
    class: "sd-issues",
    "(focusout)": "left($event)",
    "(document:pointerdown)": "outside($event)",
  },
  template: `@if (issues().length) {
    <button
      #pill
      type="button"
      class="sd-issues-pill"
      aria-haspopup="menu"
      [attr.data-tone]="tone()"
      [attr.aria-expanded]="open()"
      [attr.aria-controls]="open() ? id : null"
      (click)="toggle()"
      (keydown)="toggleKey($event)"
    >
      <span class="pill sd-issues-face" [attr.data-tone]="tone()"
        ><weave-icon name="warning" [size]="16" />{{ label() }}</span
      >
    </button>
    @if (open()) {
      <div
        class="sd-issues-list"
        role="menu"
        [id]="id"
        [attr.aria-label]="label()"
        [weaveAnchoredPopover]="open()"
        [popoverAnchor]="pill"
        (popoverClosed)="close(false)"
        (keydown)="key($event)"
      >
        @for (issue of issues(); track issue.key) {
          <button
            type="button"
            role="menuitem"
            tabindex="-1"
            [attr.data-severity]="issue.severity"
            (click)="choose(issue)"
          >
            <span class="sd-issue-head"
              ><weave-icon name="warning" [size]="16" /><span
                class="sd-issue-field"
                >{{ issue.field?.spec?.label ?? "This step" }}</span
              >
              @if (issue.status) {
                <span class="pill sd-issue-status" data-tone="danger">{{
                  issue.status
                }}</span>
              }
            </span>
            <span class="sd-issue-message">{{ issue.message }}</span>
          </button>
        }
      </div>
    }
  }`,
})
export class IssuesIndicator {
  issues = input.required<StepIssue[]>();
  chosen = output<StepIssue>();
  readonly open = signal(false);
  readonly id = `sd-issues-${++sequence}`;
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly injector = inject(Injector);

  constructor() {
    // Fixing the last issue takes the pill away, and its list with it.
    effect(() => {
      if (!this.issues().length) this.open.set(false);
    });
  }

  label(): string {
    return issueCount(this.issues());
  }
  tone(): string {
    return issueTone(this.issues());
  }
  toggle() {
    if (this.open()) return this.close();
    this.show(0);
  }
  /** ArrowDown and ArrowUp open the list on its first or last entry. */
  toggleKey(event: KeyboardEvent) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    this.show(event.key === "ArrowDown" ? 0 : -1);
  }
  private show(at: number) {
    this.open.set(true);
    afterNextRender(
      () => {
        const items = this.items();
        items[(at + items.length) % items.length]?.focus();
      },
      { injector: this.injector },
    );
  }
  choose(issue: StepIssue) {
    this.close(false);
    this.chosen.emit(issue);
  }
  key(event: KeyboardEvent) {
    const items = this.items();
    const at = items.indexOf(document.activeElement as HTMLElement);
    const move = (to: number) => {
      event.preventDefault();
      event.stopPropagation();
      items[(to + items.length) % items.length]?.focus();
    };
    if (event.key === "ArrowDown") move(at + 1);
    else if (event.key === "ArrowUp") move(at - 1);
    else if (event.key === "Home") move(0);
    else if (event.key === "End") move(items.length - 1);
    else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      this.close();
    } else if (event.key === "Tab") this.close(false);
  }
  close(returnFocus = true) {
    if (!this.open()) return;
    this.open.set(false);
    if (returnFocus)
      this.element.nativeElement
        .querySelector<HTMLElement>(".sd-issues-pill")
        ?.focus();
  }
  left(event: FocusEvent) {
    const next = event.relatedTarget;
    if (next instanceof Node && this.element.nativeElement.contains(next))
      return;
    if (next) this.close(false);
  }
  outside(event: PointerEvent) {
    if (!this.element.nativeElement.contains(event.target as Node))
      this.close(false);
  }
  private items(): HTMLElement[] {
    return [
      ...this.element.nativeElement.querySelectorAll<HTMLElement>(
        ".sd-issues-list [role=menuitem]",
      ),
    ];
  }
}
