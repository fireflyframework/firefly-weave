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
// In-app dialogs replace window.confirm()/prompt(): the desktop WKWebView
// answers those with false/null, which silently canceled every guarded action.
import {
  AfterViewInit,
  Component,
  Directive,
  ElementRef,
  Injectable,
  OnDestroy,
  inject,
  input,
  output,
  signal,
} from "@angular/core";
import { Icon } from "./icon";

const focusable =
  'button:not([disabled]), [href], input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
let sequence = 0;
// The last control focused outside a dialog. A trigger can lose focus before
// its dialog opens (for example when it is disabled while validating).
let lastFocused: HTMLElement | null = null;
document.addEventListener(
  "focusin",
  (event) => {
    if (
      event.target instanceof HTMLElement &&
      !event.target.closest(".modal-panel")
    )
      lastFocused = event.target;
  },
  true,
);

/** Moves focus to the host element once it is rendered. */
@Directive({ selector: "[weaveAutofocus]", standalone: true })
export class Autofocus implements AfterViewInit {
  private element = inject(ElementRef<HTMLElement>);
  ngAfterViewInit() {
    queueMicrotask(() => this.element.nativeElement.focus());
  }
}

/**
 * Accessible modal frame: labeled by its heading, traps Tab, closes on
 * Escape, and returns focus to the control that opened it.
 */
@Component({
  selector: "weave-modal",
  standalone: true,
  imports: [Icon],
  template: `<div class="modal-backdrop" (mousedown)="holdFocus($event)">
    <section
      class="modal-panel"
      [class.wide]="wide()"
      role="dialog"
      aria-modal="true"
      tabindex="-1"
      [attr.aria-labelledby]="titleId"
      [attr.aria-describedby]="describedBy() || null"
      (keydown)="keydown($event)"
    >
      <header class="modal-header">
        <h2 [id]="titleId">{{ heading() }}</h2>
        <button
          type="button"
          class="icon-button"
          [attr.aria-label]="closeLabel()"
          (click)="dismiss.emit()"
        >
          <weave-icon name="close" />
        </button>
      </header>
      <ng-content />
    </section>
  </div>`,
})
export class Modal implements AfterViewInit, OnDestroy {
  heading = input.required<string>();
  describedBy = input("");
  closeLabel = input("Close dialog");
  wide = input(false);
  dismiss = output<void>();
  titleId = `modal-title-${++sequence}`;
  private element = inject(ElementRef<HTMLElement>);
  private opener =
    document.activeElement instanceof HTMLElement &&
    document.activeElement !== document.body
      ? document.activeElement
      : lastFocused;
  private get panel() {
    return this.element.nativeElement.querySelector(
      ".modal-panel",
    ) as HTMLElement;
  }
  ngAfterViewInit() {
    queueMicrotask(() => {
      const panel = this.panel;
      if (!panel || panel.contains(document.activeElement)) return;
      const preferred =
        panel.querySelector<HTMLElement>("[data-initial-focus]") ??
        [...panel.querySelectorAll<HTMLElement>(focusable)].find(
          (e) => !e.closest(".modal-header"),
        );
      (preferred ?? panel).focus();
    });
  }
  ngOnDestroy() {
    const opener = this.opener;
    if (opener?.isConnected)
      queueMicrotask(() => {
        if (!document.querySelector(".modal-panel")) opener.focus();
      });
  }
  /** A press on the backdrop keeps focus in the dialog, so Tab and Escape still work. */
  holdFocus(event: MouseEvent) {
    if (event.target === event.currentTarget) event.preventDefault();
  }
  keydown(event: KeyboardEvent) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      this.dismiss.emit();
      return;
    }
    if (event.key !== "Tab") return;
    const items = [
      ...this.panel.querySelectorAll<HTMLElement>(focusable),
    ].filter((e) => e.offsetParent !== null || e === document.activeElement);
    if (!items.length) {
      event.preventDefault();
      return;
    }
    const first = items[0],
      last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
}

export interface DialogField {
  name: string;
  label: string;
  value?: string;
  placeholder?: string;
  hint?: string;
  multiline?: boolean;
  required?: boolean;
  monospace?: boolean;
  /** Returns an error message, or an empty string when the value is acceptable. */
  validate?: (value: string) => string;
}
export interface DialogOptions {
  title: string;
  message: string;
  confirmLabel: string;
  cancelLabel?: string;
  danger?: boolean;
  fields?: DialogField[];
}
export interface DialogRequest {
  options: DialogOptions;
  values: Record<string, string>;
  touched: boolean;
  resolve: (values: Record<string, string> | null) => void;
}

/** Queues confirmation and input dialogs; one is shown at a time. */
@Injectable({ providedIn: "root" })
export class DialogService {
  readonly current = signal<DialogRequest | null>(null);
  private queue: DialogRequest[] = [];
  form(options: DialogOptions): Promise<Record<string, string> | null> {
    return new Promise((resolve) => {
      const values = Object.fromEntries(
        (options.fields ?? []).map((field) => [field.name, field.value ?? ""]),
      );
      this.queue.push({ options, values, touched: false, resolve });
      if (!this.current()) this.current.set(this.queue.shift()!);
    });
  }
  async confirm(options: DialogOptions): Promise<boolean> {
    return (await this.form({ ...options, fields: [] })) !== null;
  }
  async prompt(
    options: DialogOptions & { field: DialogField },
  ): Promise<string | null> {
    const values = await this.form({ ...options, fields: [options.field] });
    return values ? values[options.field.name] : null;
  }
  settle(values: Record<string, string> | null) {
    const request = this.current();
    if (!request) return;
    this.current.set(this.queue.shift() ?? null);
    request.resolve(values);
  }
}

/** Renders the active DialogService request. Place once in the shell. */
@Component({
  selector: "weave-dialog-host",
  standalone: true,
  imports: [Modal],
  template: `@if (dialogs.current(); as request) {
    @for (key of [request]; track key) {
      <weave-modal
        [heading]="request.options.title"
        [describedBy]="messageId"
        (dismiss)="dialogs.settle(null)"
      >
        <form class="dialog-form" (submit)="submit($event, request)">
          <p [id]="messageId" class="dialog-message">
            {{ request.options.message }}
          </p>
          @for (field of request.options.fields ?? []; track field.name) {
            <label
              >{{ field.label }}
              @if (field.multiline) {
                <textarea
                  [class.monospace]="field.monospace"
                  spellcheck="false"
                  [attr.data-initial-focus]="$first ? '' : null"
                  [attr.aria-invalid]="!!error(request, field)"
                  [attr.aria-describedby]="field.hint ? hintId(field) : null"
                  [placeholder]="field.placeholder ?? ''"
                  [value]="request.values[field.name]"
                  (input)="edit(request, field, $event)"
                ></textarea>
              } @else {
                <input
                  autocomplete="off"
                  [attr.data-initial-focus]="$first ? '' : null"
                  [attr.aria-invalid]="!!error(request, field)"
                  [attr.aria-describedby]="field.hint ? hintId(field) : null"
                  [placeholder]="field.placeholder ?? ''"
                  [value]="request.values[field.name]"
                  (input)="edit(request, field, $event)"
                />
              }
            </label>
            @if (field.hint) {
              <p class="hint" [id]="hintId(field)">{{ field.hint }}</p>
            }
            @if (request.touched && error(request, field)) {
              <p class="error" role="alert">{{ error(request, field) }}</p>
            }
          }
          <div class="dialog-actions">
            <button
              type="button"
              [attr.data-initial-focus]="
                !request.options.fields?.length && request.options.danger
                  ? ''
                  : null
              "
              (click)="dialogs.settle(null)"
            >
              {{ request.options.cancelLabel ?? "Cancel" }}
            </button>
            <!-- Destructive confirmations start on Cancel; others on the action. -->
            <button
              type="submit"
              [class.primary]="!request.options.danger"
              [class.danger-solid]="request.options.danger"
              [attr.data-initial-focus]="
                request.options.fields?.length || request.options.danger
                  ? null
                  : ''
              "
              [disabled]="!valid(request)"
            >
              {{ request.options.confirmLabel }}
            </button>
          </div>
        </form>
      </weave-modal>
    }
  }`,
})
export class DialogHost {
  dialogs = inject(DialogService);
  messageId = `dialog-message-${++sequence}`;
  edit(request: DialogRequest, field: DialogField, event: Event) {
    request.touched = true;
    request.values[field.name] = (event.target as HTMLInputElement).value;
  }
  hintId(field: DialogField) {
    return `${this.messageId}-${field.name}-hint`;
  }
  error(request: DialogRequest, field: DialogField) {
    const value = request.values[field.name] ?? "";
    if (field.required && !value.trim())
      return `Enter ${/^[aeiou]/i.test(field.label) ? "an" : "a"} ${field.label.toLowerCase()}.`;
    return field.validate?.(value) ?? "";
  }
  valid(request: DialogRequest) {
    return (request.options.fields ?? []).every(
      (field) => !this.error(request, field),
    );
  }
  submit(event: Event, request: DialogRequest) {
    event.preventDefault();
    request.touched = true;
    if (!this.valid(request)) return;
    this.dialogs.settle({ ...request.values });
  }
}
