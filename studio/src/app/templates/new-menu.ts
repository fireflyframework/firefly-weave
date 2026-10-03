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
// The "New ▾" menu next to "New workflow" (WP-23/WP-20): start from a blank
// workflow, from a template, or describe an API action. A menu button with
// arrow-key navigation; Escape and Tab close it. Load it lazily (@defer).
import {
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  afterNextRender,
  inject,
  input,
  output,
  signal,
  viewChild,
} from "@angular/core";
import { Icon } from "../icon";

export type NewChoice = "blank" | "template" | "api";

let sequence = 0;

@Component({
  selector: "weave-new-menu",
  standalone: true,
  imports: [Icon],
  host: {
    class: "new-menu",
    "(focusout)": "focusOut($event)",
    "(document:pointerdown)": "outside($event)",
    "(window:resize)": "open() && position()",
  },
  template: `<button
      #toggle
      type="button"
      class="primary new-menu-toggle"
      aria-haspopup="menu"
      [attr.aria-expanded]="open()"
      [attr.aria-controls]="open() ? prefix + '-menu' : null"
      [attr.aria-label]="label()"
      (click)="toggleMenu()"
      (keydown)="toggleKey($event)"
    >
      <weave-icon name="chevron" />
    </button>
    @if (open()) {
      <div
        class="new-menu-list"
        role="menu"
        [style.left.px]="place().left"
        [style.top.px]="place().top"
        [style.width.px]="place().width"
        [id]="prefix + '-menu'"
        [attr.aria-label]="label()"
        (keydown)="menuKey($event)"
      >
        @for (item of items(); track item.id) {
          <button
            type="button"
            role="menuitem"
            tabindex="-1"
            [attr.data-choice]="item.id"
            (click)="pick(item.id)"
          >
            <weave-icon [name]="item.icon" /><span
              >{{ item.label }}<small>{{ item.hint }}</small></span
            >
          </button>
        }
      </div>
    }`,
  styles: [
    `
      :host {
        position: relative;
        display: inline-flex;
      }
      .new-menu-toggle {
        padding: 0 8px;
        min-width: 36px;
        justify-content: center;
      }
      .new-menu-toggle weave-icon {
        width: 16px;
        height: 16px;
        transform: rotate(90deg);
      }
      .new-menu-list {
        position: fixed;
        z-index: 15;
        background: var(--surface);
        border: 1px solid var(--line);
        border-radius: 8px;
        box-shadow: 0 8px 28px #173d3429;
        padding: 4px;
      }
      .new-menu-list button {
        width: 100%;
        border: 0;
        background: transparent;
        justify-content: flex-start;
        align-items: flex-start;
        text-align: left;
        gap: 10px;
        padding: 9px 10px;
        min-height: 44px;
      }
      .new-menu-list button:hover,
      .new-menu-list button:focus-visible {
        background: var(--mist);
      }
      .new-menu-list weave-icon {
        flex: none;
        color: var(--jade);
      }
      .new-menu-list small {
        display: block;
        color: var(--muted);
        font-size: 11px;
        font-weight: 400;
        white-space: normal;
      }
    `,
  ],
})
export class NewMenu {
  label = input("More ways to start");
  /** Offers "New API action" (the API action builder). */
  offerApi = input(true);
  choose = output<NewChoice>();

  prefix = `new-menu-${++sequence}`;
  open = signal(false);
  /** Where the menu opens: under the toggle, kept inside the window. */
  place = signal({ left: 0, top: 0, width: 300 });
  private toggle = viewChild<ElementRef<HTMLButtonElement>>("toggle");
  private host = inject(ElementRef<HTMLElement>);
  private injector = inject(Injector);

  constructor() {
    // The menu is fixed to the window: it follows its button when any
    // surrounding panel scrolls, and keeps focus where it is.
    const follow = () => {
      if (this.open()) this.position();
    };
    document.addEventListener("scroll", follow, {
      capture: true,
      passive: true,
    });
    inject(DestroyRef).onDestroy(() =>
      document.removeEventListener("scroll", follow, { capture: true }),
    );
  }

  items() {
    const items: {
      id: NewChoice;
      label: string;
      hint: string;
      icon: string;
    }[] = [
      {
        id: "blank",
        label: "Blank workflow",
        hint: "An empty canvas.",
        icon: "workflows",
      },
      {
        id: "template",
        label: "From a template…",
        hint: "Approval, API call, signal with a time limit and more.",
        icon: "template",
      },
    ];
    if (this.offerApi())
      items.push({
        id: "api",
        label: "New API action",
        hint: "Describe an HTTPS request or import OpenAPI; no code.",
        icon: "action",
      });
    return items;
  }
  toggleMenu() {
    if (this.open()) this.close(true);
    else this.show(0);
  }
  toggleKey(event: KeyboardEvent) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    this.show(event.key === "ArrowDown" ? 0 : -1);
  }
  menuKey(event: KeyboardEvent) {
    const entries = this.entries();
    const index = entries.indexOf(document.activeElement as HTMLElement);
    let next = -2;
    if (event.key === "ArrowDown") next = (index + 1) % entries.length;
    else if (event.key === "ArrowUp")
      next = (index - 1 + entries.length) % entries.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = entries.length - 1;
    else if (event.key === "Escape") {
      event.preventDefault();
      // Keep a surrounding panel or dialog from closing too.
      event.stopPropagation();
      this.close(true);
      return;
    } else if (event.key === "Tab") {
      this.close(false);
      return;
    }
    if (next < -1) return;
    event.preventDefault();
    entries[next]?.focus();
  }
  pick(choice: NewChoice) {
    this.close(true);
    this.choose.emit(choice);
  }
  focusOut(event: FocusEvent) {
    const next = event.relatedTarget as Node | null;
    if (
      this.open() &&
      next &&
      !(this.host.nativeElement as HTMLElement).contains(next)
    )
      this.close(false);
  }
  outside(event: PointerEvent) {
    if (
      this.open() &&
      !(this.host.nativeElement as HTMLElement).contains(event.target as Node)
    )
      this.close(false);
  }
  /** Places the menu under its button, kept inside the window. */
  position() {
    const toggle = this.toggle()?.nativeElement.getBoundingClientRect();
    if (!toggle) return;
    const viewport = document.documentElement.clientWidth;
    const width = Math.min(300, viewport - 32);
    this.place.set({
      width,
      top: toggle.bottom + 6,
      left: Math.max(16, Math.min(toggle.right - width, viewport - width - 16)),
    });
  }
  private show(index: number) {
    this.position();
    this.open.set(true);
    afterNextRender(() => this.entries().at(index)?.focus(), {
      injector: this.injector,
    });
  }
  close(returnFocus: boolean) {
    this.open.set(false);
    if (returnFocus) this.toggle()?.nativeElement.focus();
  }
  private entries() {
    return [
      ...(this.host.nativeElement as HTMLElement).querySelectorAll<HTMLElement>(
        "[role=menuitem]",
      ),
    ];
  }
}
