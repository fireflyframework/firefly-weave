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
// "Add option": a dashed menu button at the end of the form. Its menu lists
// the options not added yet, each with a one-line description; options that
// can't be added now stay listed, disabled with the reason. A filter field
// appears above eight entries. Choosing one adds the field above the button
// and focuses it; when every option is added, the button goes away.
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  inject,
  input,
  signal,
} from "@angular/core";
import { Icon } from "../../../icon";
import type { OptionEntry } from "./form-model";
import type { FormSession } from "./form-session";

let sequence = 0;

@Component({
  selector: "weave-add-option",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  host: { class: "add-option", "(focusout)": "leave($event)" },
  template: `<button
      #toggle
      type="button"
      class="add-option-button"
      aria-haspopup="menu"
      [attr.aria-expanded]="open()"
      [attr.aria-controls]="open() ? id : null"
      (click)="toggleMenu()"
      (keydown.arrowdown)="openAt(0, $event)"
    >
      <weave-icon name="plus" [size]="16" />Add option<weave-icon
        name="chevronDown"
        [size]="16"
      />
    </button>
    @if (open()) {
      <div
        class="add-option-menu"
        role="menu"
        [id]="id"
        aria-label="Add option"
        (keydown)="menuKey($event)"
      >
        @if (options().length > 8) {
          <input
            class="param-input add-option-filter"
            aria-label="Filter options"
            placeholder="Filter options"
            [value]="filter()"
            (input)="filter.set($any($event.target).value)"
          />
        }
        @for (option of shown(); track option.spec.id) {
          <button
            type="button"
            role="menuitem"
            class="add-option-item"
            [attr.aria-disabled]="option.disabled ? 'true' : null"
            (click)="choose(option)"
          >
            <b>{{ option.spec.label }}</b>
            <span>{{ option.disabled ?? option.spec.hint ?? "" }}</span>
          </button>
        } @empty {
          <p class="add-option-empty">No option matches.</p>
        }
      </div>
    }`,
})
export class AddOption {
  session = input.required<FormSession>();
  options = input.required<OptionEntry[]>();
  readonly open = signal(false);
  readonly filter = signal("");
  readonly id = `add-option-${++sequence}`;
  private readonly lifetime = inject(DestroyRef);
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  readonly shown = computed(() => {
    const words = this.filter().toLowerCase().split(/\s+/).filter(Boolean);
    return this.options().filter((o) =>
      words.every((w) =>
        `${o.spec.label} ${o.spec.hint ?? ""}`.toLowerCase().includes(w),
      ),
    );
  });

  private items(): HTMLElement[] {
    return [
      ...this.element.nativeElement.querySelectorAll<HTMLElement>(
        '[role="menuitem"]',
      ),
    ];
  }
  toggleMenu() {
    if (this.open()) return this.close();
    this.openAt(0);
  }
  openAt(index: number, event?: Event) {
    event?.preventDefault();
    this.open.set(true);
    const session = this.session();
    setTimeout(() => {
      if (
        !this.lifetime.destroyed &&
        this.open() &&
        this.session() === session &&
        session.isCurrent() &&
        this.element.nativeElement.isConnected
      )
        (
          this.element.nativeElement.querySelector<HTMLElement>(
            ".add-option-filter",
          ) ?? this.items()[index]
        )?.focus();
    });
  }

  close(focus = true) {
    this.open.set(false);
    this.filter.set("");
    if (focus)
      this.element.nativeElement
        .querySelector<HTMLElement>(".add-option-button")
        ?.focus();
  }
  menuKey(event: KeyboardEvent) {
    const items = this.items();
    const at = items.indexOf(document.activeElement as HTMLElement);
    const go = (to: number) => {
      event.preventDefault();
      items[(to + items.length) % items.length]?.focus();
    };
    if (event.key === "ArrowDown") go(at + 1);
    else if (event.key === "ArrowUp") go(at - 1);
    else if (event.key === "Home") go(0);
    else if (event.key === "End") go(items.length - 1);
    else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      this.close();
    } else if (event.key === "Tab") this.close(false);
  }
  leave(event: FocusEvent) {
    const next = event.relatedTarget as Node | null;
    if (next && !this.element.nativeElement.contains(next)) this.close(false);
  }
  choose(option: OptionEntry) {
    if (option.disabled) return;
    this.close(false);
    this.session().addOption(option.spec.id);
    this.session().announce(`Added ${option.spec.label}.`);
    const session = this.session();
    const root = this.element.nativeElement.closest<HTMLElement>(".param-form");
    if (root)
      session.focusLater(
        root,
        option.spec,
        () => {
          const field = root.querySelector<HTMLElement>(
            `[data-param="${CSS.escape(option.spec.id)}"] .param-control`,
          );
          return field ? session.firstControl(field) : null;
        },
        // The final option removes this menu; focus still belongs to its live form.
        () => this.session() === session,
      );
  }
}
