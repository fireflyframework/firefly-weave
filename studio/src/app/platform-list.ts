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
// Settings card listing saved platforms. It only emits intents; the shell
// confirms them in dialogs and calls the host. The platform in use carries
// its workspace and account actions; the others offer "Switch to this
// platform", and removal sits in each row's "⋯" menu.
import { Component, computed, input, output } from "@angular/core";
import { Icon } from "./icon";
import { RowMenu, type RowMenuItem } from "./row-menu";
import {
  ConnectionStatus,
  PlatformAction,
  PlatformView,
  accountName,
  platformServer,
  platformState,
  stateLabel,
  workspaceLabel,
} from "./connection";

@Component({
  selector: "weave-platform-list",
  standalone: true,
  imports: [Icon, RowMenu],
  template: `<section
    class="settings-card platforms-card"
    aria-labelledby="platforms-title"
  >
    <div class="card-heading">
      <h2 id="platforms-title">Platforms</h2>
      @if (storeAvailable() && activeName()) {
        <button type="button" (click)="add.emit()" [disabled]="!!busy()">
          <weave-icon name="plus" [size]="16" />Add platform
        </button>
      }
    </div>
    <p>
      Studio keeps your sign-ins in this computer's credential store. This
      window never sees your password or tokens.
    </p>
    @if (!activeName()) {
      <div class="local-line">
        <p>
          You're working locally. Workflows stay on this computer until you
          connect to a platform.
        </p>
        <button
          type="button"
          class="primary"
          [disabled]="!!busy()"
          (click)="add.emit()"
        >
          <weave-icon name="cloud" [size]="16" />Connect to a platform
        </button>
      </div>
    }
    @if (!status()) {
      <div class="notice" role="status">
        Studio couldn't load your saved platforms.
        <button type="button" (click)="reload.emit()">Try again</button>
      </div>
    } @else if (!storeAvailable()) {
      <p class="notice">
        @if (status()?.store?.location) {
          Studio can't read the saved platforms file on this computer. Check
          that it isn't damaged or locked by another program.
        } @else {
          This Studio was started with a connection file, so saved platforms
          aren't available. Start <code>weave studio</code> without
          <code>--profile</code> to save and switch platforms.
        }
      </p>
    }
    @if (platforms().length) {
      <ul class="platform-list" aria-label="Saved platforms">
        @for (platform of platforms(); track platform.name) {
          <li class="platform-row" [class.active]="isActive(platform)">
            <div class="platform-summary">
              <div class="platform-title">
                <strong>{{ platform.name }}</strong>
                @if (isActive(platform)) {
                  <span class="state-chip current">In use</span>
                  <span
                    class="state-chip"
                    [attr.data-state]="state(platform)"
                    >{{ label(platform) }}</span
                  >
                }
              </div>
              <dl class="platform-facts">
                <dt>Server</dt>
                <dd>{{ server(platform) || "Unknown" }}</dd>
                <dt>Account</dt>
                <dd>{{ accountLine(platform) }}</dd>
                <dt>Workspace</dt>
                <dd>{{ workspace(platform) || "Not chosen yet" }}</dd>
              </dl>
              @if (isActive(platform) && truncated()) {
                <p class="hint">
                  Your account can see more workspaces than Studio lists. If
                  yours is missing, ask your administrator.
                </p>
              }
            </div>
            <div class="platform-actions">
              @if (!isActive(platform)) {
                <button
                  type="button"
                  [disabled]="!!busy() || !storeAvailable()"
                  (click)="act('use', platform)"
                >
                  Switch to this platform
                </button>
              } @else {
                <button
                  type="button"
                  [disabled]="!!busy()"
                  (click)="act('workspace', platform)"
                >
                  <weave-icon name="workspace" [size]="16" />Switch workspace
                </button>
                @if (state(platform) === "signed_in") {
                  <button
                    type="button"
                    [attr.aria-label]="'Switch account on ' + platform.name"
                    [disabled]="!!busy()"
                    (click)="act('switch-account', platform)"
                  >
                    <weave-icon name="user" [size]="16" />Switch account
                  </button>
                  <button
                    type="button"
                    [attr.aria-label]="'Sign out of ' + platform.name"
                    [disabled]="!!busy()"
                    (click)="act('sign-out', platform)"
                  >
                    <weave-icon name="logout" [size]="16" />Sign out
                  </button>
                } @else {
                  <button
                    type="button"
                    class="primary"
                    [attr.aria-label]="'Sign in to ' + platform.name"
                    [disabled]="!!busy() || status()?.login_supported === false"
                    (click)="act('sign-in', platform)"
                  >
                    Sign in
                  </button>
                }
              }
              @if (storeAvailable()) {
                <weave-row-menu
                  [label]="'More actions for ' + platform.name"
                  [items]="menu(platform)"
                />
              }
              @if (busy() === platform.name) {
                <span class="loading-spinner small" role="status"
                  ><span class="sr-only">Working…</span></span
                >
              }
            </div>
          </li>
        }
      </ul>
    } @else if (storeAvailable() && activeName()) {
      <div class="platform-empty">
        <weave-icon name="cloud" />
        <p>
          No saved platforms yet. Add one to publish, run, and manage work on
          your team's Weave server.
        </p>
      </div>
    }
    @if (activeName()) {
      <div class="work-locally">
        <p>
          Stop using {{ activeName() }} and keep working on this computer only.
          Your saved platforms stay.
        </p>
        <button
          type="button"
          [disabled]="!!busy()"
          (click)="workLocally.emit()"
        >
          <weave-icon name="laptop" [size]="16" />Work locally
        </button>
      </div>
    }
  </section>`,
  styles: [
    `
      .card-heading {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        flex-wrap: wrap;
        margin-bottom: 8px;
      }
      .card-heading h2 {
        margin: 0;
      }
      .local-line {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: var(--space-3) var(--space-4);
        border: 1px solid var(--line);
        border-radius: var(--radius-md);
        background: var(--sunken);
      }
      .local-line p {
        margin: 0;
        flex: 1 1 240px;
      }
      .platform-list {
        list-style: none;
        padding: 0;
        margin: 16px 0 0;
        display: grid;
        gap: 12px;
      }
      .platform-row {
        display: flex;
        flex-wrap: wrap;
        gap: 12px 16px;
        justify-content: space-between;
        border: 1px solid var(--line);
        border-radius: var(--radius-md);
        padding: 14px 16px;
        min-width: 0;
      }
      .platform-row.active {
        border-color: var(--border-hover);
        background: var(--sunken);
        box-shadow: inset 3px 0 0 var(--accent);
      }
      .platform-summary {
        flex: 1 1 260px;
        min-width: 0;
      }
      .platform-title {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 8px;
        margin-bottom: 8px;
      }
      .platform-title strong {
        overflow-wrap: anywhere;
      }
      .platform-facts {
        grid-template-columns: 90px minmax(0, 1fr);
        gap: 4px 12px;
        margin: 0;
      }
      .platform-actions {
        display: flex;
        flex-wrap: wrap;
        align-items: flex-start;
        gap: 8px;
      }
      .platform-empty {
        display: flex;
        gap: 12px;
        align-items: center;
        color: var(--muted);
        margin-top: 12px;
      }
      .platform-empty p {
        margin: 0;
      }
      .work-locally {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        border-top: 1px solid var(--line);
        margin-top: 20px;
        padding-top: 16px;
      }
      .work-locally p {
        margin: 0;
        flex: 1 1 240px;
      }
      .notice button {
        margin-left: 8px;
      }
      /* On a phone each fact's label sits above its value, so addresses and
         accounts keep whole lines instead of breaking mid-word. */
      @media (max-width: 600px) {
        .platform-facts {
          grid-template-columns: minmax(0, 1fr);
          gap: 0;
        }
        .platform-facts dd {
          margin: 0 0 8px;
        }
      }
    `,
  ],
})
export class PlatformList {
  status = input<ConnectionStatus | null>(null);
  busy = input("");
  /** The account sees more workspaces than Studio lists. */
  truncated = input(false);
  action = output<{ action: PlatformAction; name: string }>();
  add = output<void>();
  workLocally = output<void>();
  reload = output<void>();
  storeAvailable = computed(() => this.status()?.store?.available === true);
  activeName = computed(() => this.status()?.profile?.name ?? "");
  /** Saved platforms, or the live one when Studio runs from a connection file. */
  platforms = computed<PlatformView[]>(() => {
    const status = this.status();
    const saved = status?.profiles ?? [];
    if (saved.length || !status?.profile) return saved;
    return [status.profile];
  });
  isActive(platform: PlatformView) {
    return platform.name === this.activeName();
  }
  state(platform: PlatformView) {
    return platformState(platform, this.status());
  }
  label(platform: PlatformView) {
    return stateLabel(this.state(platform));
  }
  server(platform: PlatformView) {
    return platformServer(platform);
  }
  account(platform: PlatformView) {
    if (this.isActive(platform)) {
      const status = this.status();
      return (
        accountName(status?.profile?.account) ||
        accountName(status?.authentication?.account) ||
        platform.account_label ||
        ""
      );
    }
    return platform.account_label || accountName(platform.account);
  }
  workspace(platform: PlatformView) {
    const active = this.isActive(platform)
      ? this.status()?.profile?.workspace
      : null;
    return (
      active?.label ||
      platform.workspace_label ||
      workspaceLabel(active ?? platform.workspace)
    );
  }
  /** "Signed in as jane@acme.example" or "Not signed in". */
  accountLine(platform: PlatformView) {
    const account = this.account(platform);
    if (this.isActive(platform)) {
      const state = this.state(platform);
      if (state !== "signed_in") return "Not signed in";
      return account ? `Signed in as ${account}` : "Signed in";
    }
    return account ? `Signed in as ${account}` : "Not signed in";
  }
  menu(platform: PlatformView): RowMenuItem[] {
    return [
      {
        label: `Remove ${platform.name}`,
        danger: true,
        disabled: !!this.busy(),
        run: () => this.act("remove", platform),
      },
    ];
  }
  act(action: PlatformAction, platform: PlatformView) {
    this.action.emit({ action, name: platform.name });
  }
}
