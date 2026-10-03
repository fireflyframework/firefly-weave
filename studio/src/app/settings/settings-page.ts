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
// Settings: Platforms (saved platforms, how Studio starts, keyboard controls)
// and, for administrators, People and access as its own tab. The shell (App)
// owns the state and the commands; this page loads lazily (@defer).
import { ChangeDetectionStrategy, Component, input } from "@angular/core";
import type { App } from "../app";
import { Icon } from "../icon";
import { ModalSheet, sheetWhen } from "../modal-sheet";
import { PlatformList } from "../platform-list";
import { RowMenu, type RowMenuItem } from "../row-menu";
import { statusLabel } from "../status-labels";

type Json = Record<string, unknown>;

/** One account row: the principal and the roles it holds. */
interface AccountRow {
  id: string;
  kind: string;
  active: boolean;
  /** Listed in the directory (platform administrators only). */
  listed: boolean;
  bindings: Json[];
}

@Component({
  selector: "weave-settings-page",
  changeDetection: ChangeDetectionStrategy.Eager,
  standalone: true,
  imports: [Icon, PlatformList, RowMenu, ModalSheet],
  styleUrl: "./settings-page.css",
  template: `@let h = host();
    <div class="page-heading">
      <div>
        <h1>Settings</h1>
        <p class="subtitle">
          Choose where Studio publishes and runs your work, and who can do what.
        </p>
      </div>
    </div>
    @if (showPeople()) {
      <div
        class="settings-tabs"
        role="tablist"
        aria-label="Settings sections"
        (keydown)="tabKey($event)"
      >
        @for (item of tabs; track item[0]) {
          <button
            type="button"
            role="tab"
            [id]="'settings-tab-' + item[0]"
            [attr.aria-controls]="'settings-panel-' + item[0]"
            [attr.aria-selected]="tab === item[0]"
            [attr.tabindex]="tab === item[0] ? 0 : -1"
            (click)="tab = item[0]"
          >
            {{ item[1] }}
          </button>
        }
      </div>
    }
    @if (tab === "platforms" || !showPeople()) {
      <div
        [attr.role]="showPeople() ? 'tabpanel' : null"
        [attr.id]="showPeople() ? 'settings-panel-platforms' : null"
        [attr.aria-labelledby]="showPeople() ? 'settings-tab-platforms' : null"
      >
        <weave-platform-list
          [status]="h.connectionStatus"
          [busy]="h.platformBusy"
          [truncated]="!!h.identity?.truncated"
          (action)="h.platformAction($event)"
          (add)="h.openWizard('server')"
          (workLocally)="h.workLocally()"
          (reload)="h.refreshConnectionStatus()"
        />
        @if (h.connectionStatus?.preferences) {
          <section
            class="settings-card start-preference"
            aria-labelledby="start-preference-title"
          >
            <h2 id="start-preference-title">Starting Studio</h2>
            <div
              class="start-options"
              role="radiogroup"
              aria-labelledby="start-preference-label"
              aria-describedby="start-preference-hint"
            >
              <span id="start-preference-label" class="field-label"
                >When Studio opens</span
              >
              <label class="checkbox-field"
                ><input
                  type="radio"
                  name="start-preference"
                  value="ask"
                  [checked]="h.startPreference === 'ask'"
                  (change)="h.chooseStartPreference('ask')"
                />Ask how to work</label
              >
              <label class="checkbox-field"
                ><input
                  type="radio"
                  name="start-preference"
                  value="local"
                  [checked]="h.startPreference === 'local'"
                  (change)="h.chooseStartPreference('local')"
                />Start with local authoring</label
              >
              <p class="hint" id="start-preference-hint">
                Studio asks only while no platform is saved.
                @if (!h.connectionStatus.store?.available) {
                  Saved platforms aren't available, so Studio forgets this
                  choice when it closes.
                }
              </p>
              @if (h.startError) {
                <p class="field-error" role="alert">
                  {{ h.startError.message }}
                  @if (h.startError.code) {
                    <small class="support-code"
                      >Support code: {{ h.startError.code }}</small
                    >
                  }
                </p>
              } @else if (h.startNote) {
                <p class="hint" role="status">{{ h.startNote }}</p>
              }
            </div>
          </section>
        }
        <div class="settings-card">
          <h2>Keyboard controls</h2>
          <dl>
            <dt>Undo / redo</dt>
            <dd>⌘ / Ctrl Z · Shift Z</dd>
            <dt>Save draft</dt>
            <dd>⌘ / Ctrl S</dd>
            <dt>Select step</dt>
            <dd>Arrow up / down while the canvas or outline has focus</dd>
            <dt>Cancel gesture</dt>
            <dd>Escape</dd>
            <dt>Delete step</dt>
            <dd>Delete or Backspace on the focused step</dd>
          </dl>
        </div>
      </div>
    } @else {
      <div
        role="tabpanel"
        id="settings-panel-people"
        aria-labelledby="settings-tab-people"
        class="people-layout"
        [class.with-panel]="!!h.assignAccount"
      >
        <section
          class="settings-card administration"
          aria-labelledby="people-title"
        >
          <div class="card-heading">
            <h2 id="people-title">People and access</h2>
            <div class="tool-group">
              @if (h.canManageMembers) {
                <button type="button" (click)="h.openAssign()">
                  <weave-icon name="plus" [size]="16" />Assign role
                </button>
              }
              <button
                type="button"
                class="tertiary"
                [attr.aria-disabled]="h.adminLoading ? 'true' : null"
                (click)="!h.adminLoading && h.loadAdministration()"
              >
                <weave-icon name="refresh" [size]="16" />Refresh
              </button>
            </div>
          </div>
          <p>
            Give people and apps roles here. Each role adds permissions; none
            removes them.
          </p>
          @if (h.can("grant.admin")) {
            <form
              class="principal-create-form"
              (submit)="
                $event.preventDefault(); h.administrationCommand('create')
              "
            >
              <div class="field">
                <label for="account-type">Account type</label>
                <select
                  id="account-type"
                  (change)="h.principalKind = h.value($event)"
                >
                  @for (kind of kinds; track kind) {
                    <option
                      [value]="kind"
                      [selected]="h.principalKind === kind"
                    >
                      {{ typeLabel(kind) }}
                    </option>
                  }
                </select>
              </div>
              <button
                type="submit"
                [disabled]="h.busy !== '' || h.adminUncertainCreate"
              >
                Create account
              </button>
            </form>
            @if (h.adminUncertainCreate) {
              <div class="notice" role="alert">
                <p>
                  Studio couldn't confirm the account was created. Check the
                  list before creating it again.
                </p>
                <button type="button" (click)="h.adminUncertainCreate = false">
                  I checked the list
                </button>
              </div>
            }
          }
          <h3 id="accounts-title">Accounts</h3>
          <div class="table-scroll">
            <table class="data-table" aria-labelledby="accounts-title">
              <thead>
                <tr>
                  <th scope="col">Account</th>
                  <th scope="col">Type</th>
                  <th scope="col">Roles</th>
                  <th scope="col">Applies to</th>
                  <th scope="col"><span class="sr-only">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                @for (account of accounts(); track account.id) {
                  <tr>
                    <td>
                      <strong class="account-name">{{
                        h.accountLabel(account.id)
                      }}</strong>
                      @if (!account.active) {
                        <span class="pill">Deactivated</span>
                      }
                    </td>
                    <td>{{ typeLabel(account.kind) }}</td>
                    <td>
                      <ul class="binding-list">
                        @for (
                          binding of account.bindings;
                          track binding["id"]
                        ) {
                          <li>
                            <span class="tag role-tag">{{
                              h.roleLabel(text(binding["role"]))
                            }}</span>
                          </li>
                        } @empty {
                          <li class="muted">No roles yet</li>
                        }
                      </ul>
                    </td>
                    <td>
                      <ul class="binding-list">
                        @for (
                          binding of account.bindings;
                          track binding["id"]
                        ) {
                          <li>{{ h.scopeLabel(binding["scope"]) }}</li>
                        } @empty {
                          <li class="muted">—</li>
                        }
                      </ul>
                    </td>
                    <td class="cell-actions">
                      @if (menu(account).length) {
                        <weave-row-menu
                          [label]="'Actions for ' + h.accountLabel(account.id)"
                          [items]="menu(account)"
                        />
                      }
                    </td>
                  </tr>
                } @empty {
                  <tr>
                    <td colspan="5" class="muted">
                      {{
                        h.adminLoading
                          ? "Loading accounts…"
                          : "No accounts with roles here yet."
                      }}
                    </td>
                  </tr>
                }
              </tbody>
            </table>
          </div>
          <div class="action-row">
            @if (h.adminPrincipalCursor) {
              <button
                type="button"
                (click)="h.loadAdministration('principals')"
              >
                More accounts
              </button>
            }
            @if (h.adminMemberCursor) {
              <button type="button" (click)="h.loadAdministration('members')">
                More role assignments
              </button>
            }
          </div>
          @if (h.can("grant.admin")) {
            <details class="disclosure link-sign-in">
              <summary>Advanced: link a sign-in to an account</summary>
              <form
                class="principal-link-form form-grid"
                (submit)="
                  $event.preventDefault(); h.administrationCommand('link')
                "
              >
                <div class="field">
                  <label for="link-account">Account ID</label>
                  <input
                    id="link-account"
                    class="w-key"
                    required
                    [value]="h.principalId"
                    (input)="h.principalId = h.value($event)"
                  />
                </div>
                <div class="field">
                  <label for="link-provider">Identity provider ID</label>
                  <input
                    id="link-provider"
                    required
                    [value]="h.identityProvider"
                    (input)="h.identityProvider = h.value($event)"
                  />
                </div>
                <div class="field">
                  <label for="link-issuer">Issuer URL (exact)</label>
                  <input
                    id="link-issuer"
                    type="url"
                    class="w-url"
                    required
                    [value]="h.identityIssuer"
                    (input)="h.identityIssuer = h.value($event)"
                  />
                </div>
                <div class="field">
                  <label for="link-subject">Subject (exact)</label>
                  <input
                    id="link-subject"
                    required
                    [value]="h.identitySubject"
                    (input)="h.identitySubject = h.value($event)"
                  />
                </div>
                <div class="action-row">
                  <button type="submit" [disabled]="h.busy !== ''">
                    Link sign-in
                  </button>
                </div>
              </form>
            </details>
          }
        </section>
        @if (h.assignAccount; as assign) {
          <section
            class="side-panel assign-panel"
            role="complementary"
            aria-labelledby="assign-title"
            [weaveModalSheet]="true"
            [sheetWhen]="sheetWhen.detail"
            sheetInitialFocus="#assign-title"
            (sheetDismiss)="h.assignAccount = null"
          >
            <header>
              <h2 id="assign-title" tabindex="-1">
                {{
                  assign.id
                    ? "Assign a role to " + h.accountLabel(assign.id)
                    : "Assign a role"
                }}
              </h2>
              <button
                type="button"
                class="icon-button"
                aria-label="Close assign role"
                (click)="h.assignAccount = null"
              >
                <weave-icon name="close" />
              </button>
            </header>
            <form
              class="member-grant-form"
              (submit)="
                $event.preventDefault(); h.administrationCommand('grant')
              "
            >
              @if (!assign.id) {
                <div class="field">
                  <label for="assign-account">Account ID</label>
                  <input
                    id="assign-account"
                    required
                    [value]="h.principalId"
                    (input)="h.principalId = h.value($event)"
                  />
                  <p class="field-help">
                    The account's ID, from the Accounts list or your
                    administrator.
                  </p>
                </div>
              }
              <div class="field">
                <label for="assign-role">Role</label>
                <select
                  id="assign-role"
                  (change)="h.memberRole = h.value($event)"
                >
                  @for (role of h.memberRoles; track role.value) {
                    <option
                      [value]="role.value"
                      [selected]="role.value === h.memberRole"
                    >
                      {{ role.label }}
                    </option>
                  }
                </select>
              </div>
              <div class="field">
                <label for="assign-scope">Applies to</label>
                <select
                  id="assign-scope"
                  (change)="h.memberScope = h.value($event)"
                >
                  @for (scope of scopes; track scope[0]) {
                    <option
                      [value]="scope[0]"
                      [selected]="h.memberScope === scope[0]"
                    >
                      {{ scope[1] }}
                    </option>
                  }
                </select>
                <p class="field-help">{{ scopeHelp() }}</p>
              </div>
              <div class="field">
                <label for="assign-resources"
                  >Limit to resources
                  <span class="optional">(optional)</span></label
                >
                <input
                  id="assign-resources"
                  [value]="h.memberResources"
                  (input)="h.memberResources = h.value($event)"
                  placeholder="Comma-separated resource IDs"
                />
              </div>
              <footer class="panel-footer">
                <button
                  type="submit"
                  class="primary"
                  [disabled]="h.busy !== ''"
                >
                  Assign role
                </button>
                <button type="button" (click)="h.assignAccount = null">
                  Cancel
                </button>
              </footer>
            </form>
          </section>
        }
      </div>
    }`,
})
export class SettingsPage {
  host = input.required<App>();
  readonly sheetWhen = sheetWhen;
  readonly kinds = ["human", "application", "worker"];
  readonly tabs: ["platforms" | "people", string][] = [
    ["platforms", "Platforms"],
    ["people", "People and access"],
  ];
  readonly scopes: [string, string][] = [
    ["environment", "This environment"],
    ["project", "This project"],
    ["tenant", "Whole tenant"],
  ];
  tab: "platforms" | "people" = "platforms";

  showPeople() {
    const h = this.host();
    return h.canManageMembers || h.can("grant.admin");
  }
  text(value: unknown) {
    return typeof value === "string" ? value : "";
  }
  typeLabel(kind: unknown) {
    return statusLabel("account", kind);
  }
  scopeHelp() {
    const h = this.host();
    const names = h.workspaceNames;
    if (!names) return "";
    return h.memberScope === "environment"
      ? `${names.project} / ${names.environment} only.`
      : h.memberScope === "project"
        ? `Every environment in ${names.project}.`
        : `Every project in ${names.tenant || "this tenant"}.`;
  }
  /** Accounts with their roles: the directory, plus accounts that hold roles. */
  accounts(): AccountRow[] {
    const h = this.host();
    const rows = new Map<string, AccountRow>();
    for (const principal of h.adminPrincipals) {
      const id = String(principal["id"]);
      rows.set(id, {
        id,
        kind: this.text(principal["kind"]),
        active: principal["active"] !== false,
        listed: true,
        bindings: [],
      });
    }
    for (const member of h.adminMembers) {
      const id = String(member["principal_id"]);
      const row = rows.get(id) ?? {
        id,
        kind: this.text(member["kind"]),
        active: member["active"] !== false,
        listed: false,
        bindings: [],
      };
      row.bindings.push(member);
      rows.set(id, row);
    }
    return [...rows.values()];
  }
  menu(account: AccountRow): RowMenuItem[] {
    const h = this.host();
    const busy = h.busy !== "";
    const items: RowMenuItem[] = [];
    if (h.canManageMembers)
      items.push({
        label: "Assign role",
        disabled: busy,
        run: () => h.openAssign(account.id),
      });
    if (h.can("grant.admin") && account.listed)
      items.push({
        label: account.active ? "Deactivate" : "Activate",
        danger: account.active,
        disabled: busy,
        run: () =>
          void h.administrationCommand("status", {
            id: account.id,
            kind: account.kind,
            active: account.active,
          }),
      });
    if (h.canManageMembers)
      for (const binding of account.bindings)
        items.push({
          label: `Remove ${h.roleLabel(this.text(binding["role"]))} (${h.scopeLabel(binding["scope"])})`,
          danger: true,
          disabled: busy,
          run: () => void h.administrationCommand("revoke", binding),
        });
    return items;
  }
  /** Arrow keys move between the tabs (WAI-ARIA tabs pattern). */
  tabKey(event: KeyboardEvent) {
    const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    const order = this.tabs.map((t) => t[0]);
    const index = order.indexOf(this.tab);
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? order.length - 1
          : (index + (event.key === "ArrowRight" ? 1 : -1) + order.length) %
            order.length;
    this.tab = order[next];
    queueMicrotask(() =>
      document.getElementById(`settings-tab-${this.tab}`)?.focus(),
    );
  }
}
