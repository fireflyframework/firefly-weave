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
// Settings › Preferences › My alert address: where alerts sent to the
// signed-in person as a project member go. Only they can set it, for
// themselves; the platform keeps it per tenant. A read or save lands only for
// the person and platform it was made for.
import {
  ChangeDetectionStrategy,
  Component,
  DoCheck,
  input,
  signal,
} from "@angular/core";
import type { App } from "../app";
import { describeError } from "../errors";
import { Icon } from "../icon";
import {
  readMyAlertAddress,
  saveMyAlertAddress,
  type AlertsTransport,
} from "../operate/alerts/alerts-api";
import { scopeKeyOf } from "../operate/operate-list";

const mailbox = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

@Component({
  selector: "weave-alert-address-card",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  styleUrl: "./administration.css",
  template: `<section
    class="card address-card"
    aria-labelledby="alert-address-title"
  >
    <div class="ch"><h2 id="alert-address-title">My alert address</h2></div>
    <p class="hint">
      Alerts sent to you as a project member go to this address.
    </p>
    @if (state() === "loading") {
      <p class="hint" role="status">Loading…</p>
    } @else if (state() === "error") {
      <p class="problem" role="alert">
        <weave-icon name="failCircle" [size]="16" />{{ problem() }}
      </p>
      <div class="acts">
        <button type="button" class="sm" (click)="read()">Try again</button>
      </div>
    } @else {
      <form class="form" novalidate (submit)="save($event)">
        <div class="field">
          <label for="alert-address">Address</label>
          <input
            id="alert-address"
            type="email"
            autocomplete="email"
            spellcheck="false"
            placeholder="name@example.com"
            [value]="draft()"
            [attr.aria-invalid]="problem() ? 'true' : null"
            [attr.aria-describedby]="
              problem() ? 'alert-address-problem' : 'alert-address-hint'
            "
            (input)="draft.set(value($event)); problem.set('')"
          />
          @if (problem()) {
            <p class="problem" id="alert-address-problem">
              <weave-icon name="failCircle" [size]="16" />{{ problem() }}
            </p>
          }
        </div>
        <p class="hint" id="alert-address-hint">
          {{
            saved()
              ? "Only you can set it. It's used only for alerts of projects you belong to, and no one else sees it."
              : "Until you set one, channels that list you skip you."
          }}
        </p>
        <div class="acts">
          <button
            type="submit"
            class="primary"
            [attr.aria-disabled]="busy() ? 'true' : null"
          >
            Save
          </button>
          @if (saved()) {
            <button
              type="button"
              [attr.aria-disabled]="busy() ? 'true' : null"
              (click)="remove()"
            >
              Remove
            </button>
          }
        </div>
      </form>
    }
    <p class="done" role="status">
      @if (note()) {
        <weave-icon name="ok" [size]="16" />{{ note() }}
      }
    </p>
  </section>`,
  styles: `
    :host {
      max-width: 720px;
    }
    .address-card {
      margin: var(--space-6) 0;
    }
    .form {
      display: flex;
      flex-direction: column;
      gap: var(--space-3);
    }
  `,
})
export class AlertAddressCard implements DoCheck {
  host = input.required<App>();
  readonly state = signal<"loading" | "ready" | "error">("loading");
  readonly saved = signal<string | null>(null);
  readonly draft = signal("");
  readonly problem = signal("");
  readonly note = signal("");
  readonly busy = signal(false);
  private scopeKey = "";

  private get api(): AlertsTransport {
    return this.host().api;
  }
  value(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  ngDoCheck() {
    const host = this.host();
    const key = scopeKeyOf(host);
    if (key === this.scopeKey) return;
    // Another person or platform: nothing read or typed for the last one stays.
    this.scopeKey = key;
    this.saved.set(null);
    this.draft.set("");
    this.problem.set("");
    this.note.set("");
    // A save still out for the last scope never lands, so it can't hold Save.
    this.busy.set(false);
    if (host.profile && host.identity) void this.read();
  }
  async read() {
    const key = this.scopeKey;
    this.state.set("loading");
    try {
      const mine = await readMyAlertAddress(this.api);
      if (key !== this.scopeKey) return;
      this.saved.set(mine.address);
      this.draft.set(mine.address ?? "");
      this.state.set("ready");
    } catch (error) {
      if (key !== this.scopeKey) return;
      this.problem.set(describeError(error).message);
      this.state.set("error");
    }
  }
  private async send(address: string | null) {
    const key = this.scopeKey;
    this.busy.set(true);
    this.note.set("");
    try {
      const mine = await saveMyAlertAddress(this.api, address);
      if (key !== this.scopeKey) return;
      this.saved.set(mine.address);
      this.draft.set(mine.address ?? "");
      this.note.set(
        mine.address
          ? `Saved. Alerts sent to you go to ${mine.address}.`
          : "Removed. Channels that list you skip you until you set one.",
      );
    } catch (error) {
      if (key !== this.scopeKey) return;
      this.problem.set(describeError(error).message);
    } finally {
      if (key === this.scopeKey) this.busy.set(false);
    }
  }
  save(event: Event) {
    event.preventDefault();
    if (this.busy()) return;
    const address = this.draft().trim();
    if (!mailbox.test(address)) {
      this.problem.set("Enter an email address, such as name@example.com.");
      return;
    }
    void this.send(address);
  }
  remove() {
    if (!this.busy()) void this.send(null);
  }
}
