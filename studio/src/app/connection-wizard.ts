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
// Connect to a platform: server address, review and trust, sign-in in the
// system browser (or with a device code) and the workspace choice. Every call
// goes to the paired local host; tokens never reach this page.
import {
  ChangeDetectorRef,
  Component,
  ElementRef,
  Injector,
  OnDestroy,
  OnInit,
  afterNextRender,
  inject,
  input,
  output,
  viewChild,
} from "@angular/core";
import { NgTemplateOutlet } from "@angular/common";
import { ApiError } from "./api";
import { describeError } from "./errors";
import { Icon } from "./icon";
import { WorkspacePicker } from "./workspace-picker";
import {
  AccountHint,
  ConnectProblem,
  ConnectionClient,
  ConnectionResult,
  ConnectionStatus,
  Discovery,
  Identity,
  LoginEvent,
  LoginOutcome,
  LoginStatus,
  LoginWatcher,
  SignInChoice,
  SignInFlow,
  SwitchKind,
  VerifiedPlatform,
  WizardStep,
  WorkspaceChange,
  WorkspaceOption,
  accessRequestText,
  accountHint,
  allowedFlows,
  connectProblem,
  copyText,
  existingProfileName,
  hostOf,
  isConnectionStatus,
  loginOutcome,
  optionsFromIdentity,
  parseConnectionFile,
  platformIssuer,
  platformServer,
  platformState,
  problemText,
  profileNameError,
  revocationNote,
  safeSignInUrl,
  serverAddressError,
  sessionUsable,
  signInMethods,
  signOutReport,
  stateLabel,
  workspaceKey,
} from "./connection";

type WorkspaceState =
  | "loading"
  | "ready"
  | "empty"
  | "not-linked"
  | "expired"
  | "error";

const progress: { id: WizardStep; label: string }[] = [
  { id: "server", label: "Server" },
  { id: "review", label: "Review" },
  { id: "sign-in", label: "Sign in" },
  { id: "workspace", label: "Workspace" },
];
const pending = (state: unknown) =>
  state === "starting" || state === "awaiting_user";
const record = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

@Component({
  selector: "weave-connection-wizard",
  standalone: true,
  imports: [Icon, WorkspacePicker, NgTemplateOutlet],
  template: `<section class="wizard" aria-labelledby="wizard-heading">
      @if (step !== "choice") {
        <ol class="wizard-progress" aria-label="Connection progress">
          @for (item of steps; track item.id; let i = $index) {
            <li
              [class.current]="item.id === step"
              [class.complete]="completed(item.id)"
              [attr.aria-current]="item.id === step ? 'step' : null"
            >
              <span class="step-marker" aria-hidden="true">
                @if (completed(item.id)) {
                  <weave-icon name="check" />
                } @else {
                  {{ i + 1 }}
                }
              </span>
              <span class="step-name">{{ item.label }}</span>
              @if (completed(item.id)) {
                <span class="sr-only">(completed)</span>
              }
            </li>
          }
        </ol>
      }
      <form
        class="wizard-form"
        novalidate
        (submit)="submit($event)"
        (keydown.enter)="enterKey($event)"
      >
        <div class="wizard-body">
          <h2 #heading id="wizard-heading" tabindex="-1">{{ title }}</h2>
          @switch (step) {
            @case ("choice") {
              <p class="lede">You can change this at any time in Settings.</p>
              <div class="choice-grid">
                <button
                  type="button"
                  class="choice-card"
                  aria-labelledby="choice-local-title"
                  aria-describedby="choice-local-text"
                  (click)="workLocally.emit()"
                >
                  <span class="choice-icon"><weave-icon name="laptop" /></span>
                  <span class="choice-copy"
                    ><strong id="choice-local-title">Work locally</strong
                    ><span id="choice-local-text"
                      >Draw, import, and validate workflows on this computer. No
                      account needed.</span
                    ></span
                  >
                </button>
                <button
                  type="button"
                  class="choice-card"
                  aria-labelledby="choice-platform-title"
                  aria-describedby="choice-platform-text"
                  (click)="go('server')"
                >
                  <span class="choice-icon"><weave-icon name="cloud" /></span>
                  <span class="choice-copy"
                    ><strong id="choice-platform-title"
                      >Connect to a platform</strong
                    ><span id="choice-platform-text"
                      >Sign in to your team's Weave server to publish, run, and
                      manage work.</span
                    ></span
                  >
                </button>
              </div>
            }
            @case ("server") {
              @if (removedNote) {
                <p class="notice" role="status">{{ removedNote }}</p>
              }
              <p class="lede">
                {{
                  savedPlatforms.length
                    ? "Pick a platform you saved before, or enter the address of your team's Firefly Weave server."
                    : "Enter the address of your team's Firefly Weave server."
                }}
              </p>
              @if (savedPlatforms.length) {
                <section class="saved" aria-labelledby="wizard-saved-title">
                  <h3 id="wizard-saved-title">Saved platforms</h3>
                  <ul class="saved-list">
                    @for (platform of savedPlatforms; track platform.name) {
                      <li>
                        <button
                          type="button"
                          class="saved-platform"
                          [attr.aria-label]="
                            'Use saved platform ' + platform.name
                          "
                          [disabled]="!!activating"
                          (click)="useSaved(platform.name)"
                        >
                          <weave-icon name="cloud" /><span class="saved-text"
                            ><strong>{{ platform.name }}</strong
                            ><small>{{
                              host(savedServer(platform))
                            }}</small></span
                          ><span
                            class="state-chip"
                            [attr.data-state]="platformState(platform)"
                            >{{ stateLabel(platformState(platform)) }}</span
                          >
                        </button>
                      </li>
                    }
                  </ul>
                  <p class="divider">
                    <span>Or connect to another server</span>
                  </p>
                </section>
              }
              <label for="wizard-server-address">Server address</label>
              <input
                id="wizard-server-address"
                name="server"
                type="text"
                inputmode="url"
                autocomplete="url"
                autocapitalize="off"
                spellcheck="false"
                placeholder="https://weave.example.com"
                [readOnly]="checking"
                [value]="address"
                [attr.aria-invalid]="addressError || problem ? 'true' : null"
                [attr.aria-describedby]="
                  ids(
                    'wizard-server-hint',
                    addressError && 'wizard-server-error',
                    problem && 'wizard-server-problem'
                  )
                "
                (input)="address = text($event); addressError = ''"
              />
              <p class="hint" id="wizard-server-hint">
                Your administrator can give you this address. It usually starts
                with https://.
              </p>
              @if (addressError) {
                <p class="field-error" id="wizard-server-error">
                  {{ addressError }}
                </p>
              }
              @if (checking) {
                <div class="progress-line" role="status">
                  <span class="loading-spinner small" aria-hidden="true"></span
                  ><span>Checking {{ host(address) || "the server" }}…</span
                  ><button type="button" (click)="cancelCheck()">Cancel</button>
                </div>
              }
              @if (activating) {
                <div class="progress-line" role="status">
                  <span class="loading-spinner small" aria-hidden="true"></span
                  ><span>Opening {{ activating }}…</span>
                </div>
              }
              @if (problem) {
                <ng-container
                  *ngTemplateOutlet="
                    problemBlock;
                    context: { $implicit: problem, id: 'wizard-server-problem' }
                  "
                />
              }
              <details class="advanced">
                <summary>Advanced</summary>
                <p>
                  Has your administrator given you a connection file instead of
                  an address?
                </p>
                <button type="button" (click)="openFile()">
                  Use a connection file
                </button>
              </details>
            }
            @case ("review") {
              @if (fileMode) {
                <p class="lede">
                  Choose the connection file your administrator gave you. It
                  holds sign-in settings only, never passwords or tokens.
                </p>
                <div
                  class="file-drop"
                  [class.dragging]="dragging"
                  (dragover)="$event.preventDefault(); dragging = true"
                  (dragleave)="dragging = false"
                  (drop)="dropFile($event)"
                >
                  <input
                    #filePicker
                    type="file"
                    accept=".json,application/json"
                    aria-label="Connection file"
                    hidden
                    (change)="chooseFile($event)"
                  />
                  <button
                    type="button"
                    id="wizard-file-button"
                    [attr.aria-describedby]="
                      ids('wizard-file-hint', fileError && 'wizard-file-error')
                    "
                    (click)="filePicker.click()"
                  >
                    <weave-icon name="upload" />{{
                      fileName
                        ? "Choose another file"
                        : "Choose connection file"
                    }}
                  </button>
                  <span class="hint" id="wizard-file-hint">{{
                    fileName
                      ? fileName
                      : "Or drop a .json file here · up to 64 KiB"
                  }}</span>
                </div>
                @if (fileError) {
                  <p class="field-error" id="wizard-file-error" role="alert">
                    {{ fileError }}
                  </p>
                }
                @if (fileLogin) {
                  <dl class="review-list">
                    <dt>Server</dt>
                    <dd>{{ fileLogin["target"] }}</dd>
                    <dt>Sign-in</dt>
                    <dd>
                      You will sign in with {{ fileLogin["provider_id"] }} at
                      <strong>{{ host(str(fileLogin["issuer"])) }}</strong
                      >.
                    </dd>
                    <dt>Client ID</dt>
                    <dd>{{ fileLogin["client_id"] }}</dd>
                    <dt>Scopes</dt>
                    <dd>{{ list(fileLogin["scopes"]) || "None" }}</dd>
                    <dt>Extra trusted origins</dt>
                    <dd>
                      {{
                        list(fileLogin["trusted_endpoint_origins"]) || "None"
                      }}
                    </dd>
                  </dl>
                  <ng-container
                    *ngTemplateOutlet="
                      nameAndTrust;
                      context: { $implicit: host(str(fileLogin['issuer'])) }
                    "
                  />
                }
              } @else if (discovery) {
                <p class="lede">
                  Check these details before Studio connects. Continue only if
                  you recognize them.
                </p>
                @if (existingName) {
                  <div class="notice saved-notice">
                    <p>
                      You already saved this server as
                      <strong>{{ existingName }}</strong
                      >.
                    </p>
                    <button
                      type="button"
                      [disabled]="!!activating"
                      (click)="useSaved(existingName)"
                    >
                      Use saved platform {{ existingName }}
                    </button>
                  </div>
                }
                <dl class="review-list">
                  <dt>Platform</dt>
                  <dd>
                    {{ discovery.display_name || host(discovery.server) }}
                  </dd>
                  <dt>Server</dt>
                  <dd>{{ discovery.server }}</dd>
                  @if (option) {
                    <dt>Sign-in</dt>
                    <dd>
                      You will sign in with {{ option.display_name }} at
                      <strong>{{ issuerHost(option) }}</strong
                      >.
                    </dd>
                    <dt>Sign-in methods</dt>
                    <dd>{{ methodsText(option) }}</dd>
                  }
                </dl>
                @if (discovery.sign_in.length > 1) {
                  <fieldset class="provider-options">
                    <legend>Identity provider</legend>
                    @for (
                      choice of discovery.sign_in;
                      track choice.provider_id
                    ) {
                      <label
                        class="radio-card"
                        [class.selected]="choice.provider_id === providerId"
                        [class.unavailable]="!!choice.problem"
                      >
                        <input
                          type="radio"
                          name="wizard-provider"
                          [value]="choice.provider_id"
                          [checked]="choice.provider_id === providerId"
                          [disabled]="!!choice.problem || savedHere"
                          [attr.aria-describedby]="'wizard-provider-' + $index"
                          (change)="chooseProvider(choice)"
                        /><span class="radio-copy"
                          ><strong>{{ choice.display_name }}</strong
                          ><small [id]="'wizard-provider-' + $index"
                            >{{ issuerHost(choice) }} ·
                            {{ methodsText(choice) }}
                            @if (choice.problem) {
                              <span class="field-error">{{
                                problemText(choice.problem)
                              }}</span>
                            }
                          </small></span
                        >
                      </label>
                    }
                  </fieldset>
                } @else if (option && option.problem) {
                  <div class="problem" role="alert">
                    <weave-icon name="warning" />
                    <div>
                      <strong
                        >You can't sign in with {{ option.display_name }} right
                        now.</strong
                      >
                      <p>{{ problemText(option.problem) }}</p>
                      @if (problemCode(option.problem)) {
                        <small class="support-code"
                          >Support code:
                          {{ problemCode(option.problem) }}</small
                        >
                      }
                    </div>
                  </div>
                }
                <ng-container
                  *ngTemplateOutlet="
                    nameAndTrust;
                    context: { $implicit: option ? issuerHost(option) : '' }
                  "
                />
                <details class="advanced">
                  <summary>Advanced</summary>
                  @if (option) {
                    <dl class="review-list">
                      <dt>Identity provider address</dt>
                      <dd>{{ option.issuer }}</dd>
                      <dt>Client ID</dt>
                      <dd>{{ option.client_id }}</dd>
                      <dt>Scopes</dt>
                      <dd>{{ option.scopes.join(" ") }}</dd>
                      <dt>Extra trusted origins</dt>
                      <dd>
                        {{
                          option.trusted_endpoint_origins.join(", ") || "None"
                        }}
                      </dd>
                    </dl>
                  }
                  <button type="button" (click)="openFile()">
                    Use a connection file instead
                  </button>
                </details>
              }
              @if (saveProblem) {
                <ng-container
                  *ngTemplateOutlet="
                    problemBlock;
                    context: {
                      $implicit: saveProblem,
                      id: 'wizard-save-problem',
                    }
                  "
                />
              }
              @if (saving) {
                <div class="progress-line" role="status">
                  <span class="loading-spinner small" aria-hidden="true"></span
                  ><span>Saving {{ name }}…</span>
                </div>
              }
            }
            @case ("sign-in") {
              @if (loginUnsupported) {
                <div class="notice">
                  <p>
                    Sign-in for {{ platformLabel }} is managed by the weave
                    command line. Run <code>weave auth login</code> in a
                    terminal, then check again.
                  </p>
                </div>
                <div class="action-row">
                  <button type="submit" class="primary">Check again</button>
                </div>
              } @else if (waiting) {
                <div class="waiting" role="status">
                  <span class="loading-spinner small" aria-hidden="true"></span
                  ><strong>{{
                    login?.state === "starting"
                      ? "Starting sign-in…"
                      : "Waiting for you to finish signing in…"
                  }}</strong>
                </div>
                @if (deviceCode) {
                  <p>
                    On this or any other device, open
                    <strong>{{ host(signInUrl) || "the sign-in page" }}</strong>
                    and enter this code:
                  </p>
                  <div class="device-code-row">
                    <output
                      class="device-code"
                      id="wizard-device-code"
                      aria-label="Sign-in code"
                      >{{ deviceCode }}</output
                    ><button
                      type="button"
                      (click)="copy(deviceCode, 'wizard-device-code')"
                    >
                      <weave-icon name="copy" />Copy code
                    </button>
                  </div>
                } @else if (signInUrl) {
                  <ol class="instructions">
                    <li>
                      {{
                        desktop()
                          ? "Studio opened the sign-in page in your web browser."
                          : "Open the sign-in page. It opens in a new tab."
                      }}
                    </li>
                    <li>
                      Sign in{{ providerLabel ? " with " + providerLabel : "" }}
                      and approve access.
                    </li>
                    <li>Come back to Studio. This page continues by itself.</li>
                  </ol>
                }
                @if (signInUrl) {
                  <div class="action-row">
                    @if (desktop()) {
                      <button type="button" (click)="openAgain()">
                        <weave-icon name="external" />Open again
                      </button>
                    } @else {
                      <a
                        class="button"
                        [class.primary-link]="!deviceCode"
                        [href]="signInUrl"
                        target="_blank"
                        rel="noopener noreferrer"
                        ><weave-icon name="external" />Open sign-in page</a
                      >
                    }
                  </div>
                  <label for="wizard-sign-in-address">Sign-in address</label>
                  <div class="copy-field">
                    <input
                      id="wizard-sign-in-address"
                      readonly
                      spellcheck="false"
                      [value]="signInUrl"
                    /><button
                      type="button"
                      aria-label="Copy sign-in address"
                      (click)="copy(signInUrl, 'wizard-sign-in-address')"
                    >
                      <weave-icon name="copy" />Copy
                    </button>
                  </div>
                }
                @if (loginNotice) {
                  <p class="notice">{{ loginNotice }}</p>
                }
                @if (openNote) {
                  <p class="field-error" role="alert">{{ openNote }}</p>
                }
                <p class="hint">
                  {{
                    remaining
                      ? remaining + " to finish signing in."
                      : "Finish signing in within a few minutes."
                  }}
                </p>
                <div class="action-row">
                  <button
                    type="button"
                    [disabled]="cancelling"
                    (click)="cancelSignIn()"
                  >
                    Cancel sign-in
                  </button>
                </div>
              } @else if (outcome) {
                <ng-container
                  *ngTemplateOutlet="
                    problemBlock;
                    context: {
                      $implicit: signInProblem,
                      id: 'wizard-sign-in-problem',
                    }
                  "
                />
                @if (retryFlow) {
                  <div class="action-row">
                    <button type="submit" class="primary">
                      <weave-icon name="refresh" />Try again
                    </button>
                  </div>
                }
              } @else if (!signInFlows.length) {
                <div class="notice">
                  <p>
                    {{ platformLabel }} doesn't allow a sign-in method Studio
                    can use. Ask your administrator to check the platform's
                    sign-in settings.
                  </p>
                </div>
              } @else {
                <p class="lede">
                  Sign in to <strong>{{ platformLabel }}</strong
                  >{{ providerLabel ? " with " + providerLabel : "" }}. Your
                  password stays with
                  {{ issuerLabel || "your identity provider" }}; Studio never
                  sees it.
                </p>
                @if (switching) {
                  <p class="notice">
                    Choose a different account on the sign-in page.
                  </p>
                }
                <div class="action-row">
                  @if (browserAllowed) {
                    <button type="submit" class="primary" [disabled]="starting">
                      <weave-icon name="external" />Sign in with your browser
                    </button>
                    @if (deviceAllowed) {
                      <button
                        type="button"
                        [disabled]="starting"
                        (click)="startLogin('device')"
                      >
                        Use a code instead
                      </button>
                    }
                  } @else {
                    <button type="submit" class="primary" [disabled]="starting">
                      Sign in with a code
                    </button>
                  }
                </div>
                @if (!browserAllowed) {
                  <p class="hint">
                    Your administrator allows sign-in with a code only. You
                    enter it on a sign-in page, on this or any other device.
                  </p>
                } @else if (deviceAllowed) {
                  <p class="hint">
                    Use a code if this computer can't open a web browser, or to
                    sign in on your phone.
                  </p>
                }
              }
              @if (copyNote) {
                <p class="hint" role="status">{{ copyNote }}</p>
              }
            }
            @case ("workspace") {
              @switch (workspaceState) {
                @case ("loading") {
                  <div class="waiting" role="status">
                    <span
                      class="loading-spinner small"
                      aria-hidden="true"
                    ></span
                    ><span>Loading your workspaces…</span>
                  </div>
                }
                @case ("ready") {
                  <p class="lede">
                    Choose where Studio publishes and runs your work on
                    <strong>{{ platformLabel }}</strong
                    >. You can change it later.
                  </p>
                  @if (workspaceRevoked) {
                    <p class="notice">
                      The workspace you used before is no longer available to
                      your account. Choose another one.
                    </p>
                  }
                  <weave-workspace-picker
                    [options]="workspaces"
                    [selected]="selectedKey"
                    [truncated]="truncated"
                    [invalid]="!!workspaceError"
                    [describedBy]="
                      workspaceError ? 'wizard-workspace-error' : ''
                    "
                    (selectedChange)="pickWorkspace($event)"
                  />
                  @if (workspaceError) {
                    <p
                      class="field-error"
                      id="wizard-workspace-error"
                      role="alert"
                    >
                      {{ workspaceError }}
                    </p>
                  }
                  @if (selecting) {
                    <div class="progress-line" role="status">
                      <span
                        class="loading-spinner small"
                        aria-hidden="true"
                      ></span
                      ><span>Opening the workspace…</span>
                    </div>
                  }
                }
                @case ("empty") {
                  <div class="access-state">
                    <weave-icon name="lock" />
                    <div>
                      <h3>
                        Your account doesn't have access to a workspace yet
                      </h3>
                      <p>
                        You're signed in, but no project on {{ platformLabel }}
                        is shared with your account. Ask your administrator to
                        give you access, and send them these details.
                      </p>
                    </div>
                  </div>
                  <ng-container *ngTemplateOutlet="accessBlock" />
                }
                @case ("not-linked") {
                  <div class="access-state">
                    <weave-icon name="user" />
                    <div>
                      <h3>
                        You signed in, but this platform doesn't recognize your
                        account yet
                      </h3>
                      <p>
                        Your administrator needs to link your sign-in to a Weave
                        account on {{ platformLabel }}. Send them these details.
                      </p>
                    </div>
                  </div>
                  <ng-container *ngTemplateOutlet="accessBlock" />
                  <div class="action-row">
                    <button type="button" (click)="signInAgain(true)">
                      <weave-icon name="user" />Sign in with a different account
                    </button>
                  </div>
                }
                @case ("expired") {
                  <div class="access-state">
                    <weave-icon name="lock" />
                    <div>
                      <h3>Your sign-in for {{ platformLabel }} ended</h3>
                      <p>Sign in again to choose a workspace.</p>
                    </div>
                  </div>
                  <div class="action-row">
                    <button
                      type="button"
                      class="primary"
                      (click)="signInAgain(false)"
                    >
                      Sign in again
                    </button>
                  </div>
                }
                @case ("error") {
                  @if (workspaceProblem) {
                    <ng-container
                      *ngTemplateOutlet="
                        problemBlock;
                        context: {
                          $implicit: workspaceProblem,
                          id: 'wizard-workspace-problem',
                        }
                      "
                    />
                  }
                  <div class="action-row">
                    <button type="button" (click)="loadWorkspaces()">
                      <weave-icon name="refresh" />Try again
                    </button>
                  </div>
                }
              }
              @if (workspaceSaveProblem) {
                <ng-container
                  *ngTemplateOutlet="
                    problemBlock;
                    context: {
                      $implicit: workspaceSaveProblem,
                      id: 'wizard-workspace-save-problem',
                    }
                  "
                />
              }
              @if (copyNote) {
                <p class="hint" role="status">{{ copyNote }}</p>
              }
            }
          }
        </div>
        @if (step !== "choice") {
          <footer class="wizard-footer">
            <button type="button" [disabled]="cancelling" (click)="back()">
              {{ history.length ? "Back" : "Cancel" }}
            </button>
            @if (primaryLabel) {
              <button type="submit" class="primary" [disabled]="primaryBusy">
                {{ primaryLabel }}
              </button>
            }
          </footer>
        }
      </form>
    </section>
    <ng-template #nameAndTrust let-issuer>
      @if (savedHere) {
        <label for="wizard-name">Name</label>
        <input
          id="wizard-name"
          name="name"
          readonly
          autocomplete="off"
          spellcheck="false"
          aria-describedby="wizard-saved-note"
          [value]="created"
        />
        <div class="notice saved-notice" id="wizard-saved-note">
          <p>
            Saved as <strong>{{ created }}</strong
            >. To change its name or sign-in settings, remove it and start over.
          </p>
          <button
            type="button"
            [disabled]="removing"
            (click)="removeAndStartOver()"
          >
            Remove and start over
          </button>
        </div>
        @if (removing) {
          <div class="progress-line" role="status">
            <span class="loading-spinner small" aria-hidden="true"></span
            ><span>Removing {{ created }}…</span>
          </div>
        }
      } @else {
        <ng-container
          *ngTemplateOutlet="editableName; context: { $implicit: issuer }"
        />
      }
    </ng-template>
    <ng-template #editableName let-issuer>
      <label for="wizard-name">Name</label>
      <input
        id="wizard-name"
        name="name"
        maxlength="64"
        autocomplete="off"
        autocapitalize="off"
        spellcheck="false"
        [value]="name"
        [attr.aria-invalid]="nameError ? 'true' : null"
        [attr.aria-describedby]="
          ids('wizard-name-hint', nameError && 'wizard-name-error')
        "
        (input)="name = text($event); nameError = ''"
      />
      <p class="hint" id="wizard-name-hint">
        How this platform appears in Studio and in the weave command line on
        this computer.
      </p>
      @if (nameError) {
        <p class="field-error" id="wizard-name-error">{{ nameError }}</p>
      }
      <label class="checkbox-field trust-field"
        ><input
          type="checkbox"
          id="wizard-trust"
          [checked]="trusted"
          [attr.aria-invalid]="trustError ? 'true' : null"
          [attr.aria-describedby]="
            ids('wizard-trust-hint', trustError && 'wizard-trust-error')
          "
          (change)="trusted = checked($event); trustError = ''"
        />I trust this server and identity provider</label
      >
      <p class="hint" id="wizard-trust-hint">
        Studio sends you only to {{ issuer || "this identity provider" }} to
        sign in. Your password never passes through Studio.
      </p>
      @if (trustError) {
        <p class="field-error" id="wizard-trust-error">{{ trustError }}</p>
      }
    </ng-template>
    <ng-template #accessBlock>
      <label for="wizard-access-details">Details for your administrator</label>
      <textarea
        id="wizard-access-details"
        class="access-details monospace"
        readonly
        rows="6"
        spellcheck="false"
        [value]="accessDetails"
        [selectionStart]="0"
        [selectionEnd]="0"
      ></textarea>
      <div class="action-row">
        <button
          type="button"
          (click)="copy(accessDetails, 'wizard-access-details')"
        >
          <weave-icon name="copy" />Copy details
        </button>
        <button type="button" (click)="loadWorkspaces()">
          <weave-icon name="refresh" />Check again
        </button>
      </div>
    </ng-template>
    <ng-template #problemBlock let-p let-id="id">
      <!-- A canceled sign-in is a neutral note, not a red error. -->
      <div
        class="problem"
        [class.calm]="p.calm"
        [id]="id"
        [attr.role]="p.calm ? 'status' : 'alert'"
      >
        <weave-icon [name]="p.calm ? 'help' : 'warning'" />
        <div>
          <strong>{{ p.title }}</strong>
          <p>{{ p.hint }}</p>
          @if (p.action) {
            <button type="button" (click)="problemAction(p.action)">
              {{ p.action.label }}
            </button>
          }
          @if (p.code) {
            <small class="support-code">Support code: {{ p.code }}</small>
          }
        </div>
      </div>
    </ng-template>`,
  styles: [
    `
      :host {
        display: block;
        min-width: 0;
      }
      .wizard {
        max-width: 760px;
        margin: 0 auto;
        background: var(--surface);
        border: 1px solid var(--line);
        border-radius: 12px;
        min-width: 0;
        /* Rounds the footer's corners with the card; clip keeps it sticky. */
        overflow: clip;
      }
      .wizard-progress {
        list-style: none;
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 8px;
        margin: 0;
        padding: 20px 28px 0;
      }
      .wizard-progress li {
        display: flex;
        align-items: center;
        gap: 8px;
        min-width: 0;
        font-size: 12px;
        color: var(--muted);
      }
      .wizard-progress li.current {
        color: var(--text);
        font-weight: 700;
      }
      .step-marker {
        flex: none;
        width: 26px;
        height: 26px;
        border-radius: 50%;
        border: 1px solid var(--border);
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-weight: 700;
        background: var(--surface);
      }
      .step-marker weave-icon {
        width: 16px;
        height: 16px;
        --icon-stroke: 1.5;
      }
      li.current .step-marker {
        background: var(--accent);
        border-color: var(--accent);
        color: var(--on-accent);
      }
      li.complete .step-marker {
        background: var(--success-bg);
        border-color: var(--success-bd);
        color: var(--success-ink);
      }
      .step-name {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .wizard-body {
        padding: 24px 28px 8px;
        min-width: 0;
      }
      .wizard-body h2 {
        font-size: 20px;
        margin-bottom: 8px;
        overflow-wrap: anywhere;
      }
      .wizard-body h2:focus {
        outline: none;
      }
      /* The heading takes focus only so screen readers announce the step. */
      .wizard-body h3 {
        margin: 0 0 6px;
        font-size: 14px;
      }
      .lede {
        color: var(--muted);
        margin-bottom: 20px;
        overflow-wrap: anywhere;
      }
      .wizard-body > label,
      .wizard-body label[for] {
        margin-top: 16px;
      }
      .wizard-body input:not([type="checkbox"]):not([type="radio"]) {
        margin-top: 8px;
      }
      .hint {
        margin: 6px 0 0;
      }
      .field-error {
        color: var(--danger);
        font-size: 12px;
        margin: 6px 0 0;
      }
      .choice-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 16px;
        margin: 8px 0 24px;
      }
      .choice-card {
        display: flex;
        align-items: flex-start;
        justify-content: flex-start;
        gap: 14px;
        text-align: left;
        white-space: normal;
        padding: 20px;
        min-height: 128px;
        border-radius: 10px;
        border-color: var(--border);
      }
      .choice-card:hover {
        border-color: var(--border-hover);
      }
      .choice-icon {
        flex: none;
        width: 40px;
        height: 40px;
        border-radius: 10px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        background: var(--raised);
        color: var(--text);
      }
      .choice-copy {
        display: grid;
        gap: 6px;
        min-width: 0;
      }
      .choice-copy strong {
        font-size: 15px;
      }
      .choice-copy span {
        color: var(--muted);
        font-weight: 400;
        line-height: 1.5;
      }
      .saved {
        margin-bottom: 8px;
      }
      .saved-list {
        list-style: none;
        margin: 0;
        padding: 0;
        display: grid;
        gap: 8px;
      }
      .saved-platform {
        width: 100%;
        justify-content: flex-start;
        text-align: left;
        white-space: normal;
        padding: 10px 12px;
        gap: 12px;
      }
      .saved-text {
        display: grid;
        flex: 1;
        min-width: 0;
      }
      .saved-text strong,
      .saved-text small {
        overflow-wrap: anywhere;
      }
      .saved-text small {
        color: var(--muted);
        font-weight: 400;
      }
      .divider {
        display: flex;
        align-items: center;
        gap: 12px;
        color: var(--muted);
        font-size: 12px;
        margin: 20px 0 0;
      }
      .divider::before,
      .divider::after {
        content: "";
        flex: 1;
        border-top: 1px solid var(--line);
      }
      .progress-line,
      .waiting {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
      }
      .waiting {
        flex-wrap: nowrap;
        margin-bottom: 12px;
      }
      .progress-line,
      .waiting {
        gap: 8px 10px;
        margin: 16px 0 0;
        font-size: 13px;
      }
      .progress-line .loading-spinner,
      .waiting .loading-spinner {
        margin: 0;
        padding: 0;
        background: none;
      }
      .problem {
        display: flex;
        gap: 12px;
        margin: 16px 0 0;
        padding: 14px 16px;
        border: 1px solid var(--danger-bd);
        border-radius: 8px;
        background: var(--danger-bg);
        color: var(--danger-ink);
        min-width: 0;
      }
      .problem > weave-icon {
        color: var(--danger);
        margin-top: 1px;
      }
      .problem.calm {
        border-color: var(--neutral-bd);
        background: var(--neutral-bg);
        color: var(--text);
      }
      .problem.calm > weave-icon {
        color: var(--muted);
      }
      .problem > div {
        min-width: 0;
        flex: 1;
      }
      .problem p {
        margin: 4px 0 8px;
        font-size: 13px;
        overflow-wrap: anywhere;
      }
      .problem button {
        white-space: normal;
        text-align: left;
      }
      .advanced {
        margin: 20px 0 0;
        border-top: 1px solid var(--line);
        padding-top: 12px;
      }
      .advanced summary {
        cursor: pointer;
        font-weight: 600;
        font-size: 13px;
        width: fit-content;
      }
      .advanced p {
        font-size: 13px;
        color: var(--muted);
        margin: 10px 0 8px;
      }
      .advanced .review-list {
        margin-top: 12px;
      }
      .review-list {
        grid-template-columns: minmax(110px, 170px) minmax(0, 1fr);
        margin: 0 0 8px;
        font-size: 13px;
      }
      .provider-options {
        border: 0;
        padding: 0;
        margin: 16px 0 0;
        min-width: 0;
      }
      .provider-options legend {
        font-size: 12px;
        font-weight: 600;
        margin-bottom: 8px;
      }
      .radio-card {
        display: flex;
        gap: 10px;
        align-items: flex-start;
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 10px 12px;
        margin-bottom: 8px;
        cursor: pointer;
        font-weight: 400;
      }
      .radio-card.selected {
        border-color: var(--accent);
        background: var(--selected);
      }
      .radio-card.unavailable {
        cursor: not-allowed;
        background: var(--disabled-bg);
      }
      .radio-card input {
        width: 16px;
        height: 16px;
        margin: 2px 0 0;
        flex: none;
        accent-color: var(--accent);
      }
      .radio-copy {
        display: grid;
        gap: 2px;
        min-width: 0;
      }
      .radio-copy small {
        color: var(--muted);
        overflow-wrap: anywhere;
      }
      .radio-copy .field-error {
        display: block;
        margin-top: 4px;
      }
      .trust-field {
        margin-top: 20px;
        font-size: 13px;
        align-items: flex-start;
      }
      .trust-field input {
        margin: 2px 0 0;
        flex: none;
      }
      .saved-notice {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        justify-content: space-between;
        gap: 8px 12px;
        margin-bottom: 16px;
      }
      .saved-notice p {
        margin: 0;
      }
      .saved-notice button {
        white-space: normal;
      }
      .file-drop {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 10px 14px;
        border: 1px dashed var(--field-border);
        border-radius: 10px;
        background: var(--sunken);
        padding: 18px;
        margin-bottom: 16px;
      }
      .file-drop.dragging {
        border-color: var(--accent);
        background: var(--selected);
      }
      .file-drop .hint {
        margin: 0;
        overflow-wrap: anywhere;
        min-width: 0;
      }
      .action-row {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin: 16px 0 0;
      }
      .action-row button,
      .action-row .button {
        white-space: normal;
      }
      a.button {
        text-decoration: none;
      }
      a.button.primary-link {
        background: var(--accent);
        border-color: var(--accent);
        color: var(--on-accent);
      }
      a.button.primary-link:hover {
        background: var(--accent-hover);
      }
      .instructions {
        margin: 12px 0 0;
        padding-left: 20px;
        line-height: 1.7;
      }
      .copy-field {
        display: flex;
        gap: 8px;
        align-items: flex-end;
        min-width: 0;
      }
      .copy-field input {
        flex: 1;
        min-width: 0;
        font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
        font-size: 12px;
      }
      .device-code-row {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 12px;
        margin: 8px 0 16px;
      }
      .device-code {
        font:
          650 28px/1.2 ui-monospace,
          SFMono-Regular,
          Consolas,
          monospace;
        letter-spacing: 0.12em;
        padding: 12px 18px;
        border-radius: 8px;
        background: var(--raised);
        border: 1px solid var(--line);
        overflow-wrap: anywhere;
        max-width: 100%;
      }
      .access-state {
        display: flex;
        gap: 14px;
        align-items: flex-start;
        margin-bottom: 12px;
      }
      .access-state > weave-icon {
        flex: none;
        width: 36px;
        height: 36px;
        padding: 8px;
        border-radius: 10px;
        background: var(--warning-bg);
        color: var(--warning-ink);
      }
      .access-state p {
        color: var(--muted);
        margin: 0;
      }
      .access-details {
        width: 100%;
        margin-top: 8px;
        min-height: 120px;
        resize: vertical;
      }
      .wizard-footer {
        position: sticky;
        bottom: 0;
        z-index: 1;
        display: flex;
        justify-content: space-between;
        gap: 8px;
        margin-top: 16px;
        padding: 14px 28px;
        border-top: 1px solid var(--line);
        /* Square while it floats over the step, so nothing shows through. */
        border-radius: 0;
        background: var(--surface);
      }
      @media (max-width: 640px) {
        .wizard-progress {
          display: flex;
          padding: 16px 16px 0;
          gap: 6px;
        }
        .wizard-progress li {
          flex: none;
        }
        .wizard-progress li.current {
          flex: 1;
        }
        .wizard-progress li:not(.current) .step-name {
          position: absolute;
          width: 1px;
          height: 1px;
          overflow: hidden;
          clip: rect(0, 0, 0, 0);
          white-space: nowrap;
        }
        .wizard-body {
          padding: 20px 16px 8px;
        }
        .wizard-footer {
          padding: 12px 16px;
        }
        .review-list {
          grid-template-columns: minmax(0, 1fr);
          gap: 2px 0;
        }
        .review-list dd {
          margin-bottom: 10px;
        }
        .device-code {
          font-size: 22px;
        }
      }
      @media (max-height: 640px) {
        .wizard-body {
          padding-top: 16px;
        }
        .wizard-progress {
          padding-top: 14px;
        }
        .wizard-footer {
          padding-top: 10px;
          padding-bottom: 10px;
        }
      }
    `,
  ],
})
export class ConnectionWizard implements OnInit, OnDestroy {
  private cdr = inject(ChangeDetectorRef);
  private injector = inject(Injector);
  client = input.required<ConnectionClient>();
  status = input<ConnectionStatus | null>(null);
  start = input<WizardStep>("server");
  /** Saved platform this wizard signs in to, when opened from Settings or the menu. */
  platform = input("");
  server = input("");
  switchAccount = input(false);
  desktop = input(false);
  /**
   * The shell's sign-in follower. The wizard shows its sign-in while open;
   * the shell keeps following it after the wizard closes.
   */
  watcher = input<LoginWatcher | null>(null);
  currentWorkspace = input<{
    tenantId: string | null;
    projectId: string | null;
    environmentId: string | null;
  } | null>(null);
  /** Asks the person before a switch would detach unsaved platform work. */
  confirmSwitch = input<(kind: SwitchKind) => Promise<boolean>>(
    async () => true,
  );
  switched = output<ConnectionResult>();
  statusChange = output<ConnectionStatus>();
  verified = output<VerifiedPlatform>();
  workspaceChange = output<WorkspaceChange>();
  finish = output<void>();
  cancel = output<void>();
  workLocally = output<void>();
  /**
   * The signed-in account can't be used here (not linked, or its sign-in
   * ended): the shell forgets the identity it held for the previous account.
   */
  accountProblem = output<"not-linked" | "expired">();
  heading = viewChild<ElementRef<HTMLElement>>("heading");

  steps = progress;
  step: WizardStep = "server";
  history: WizardStep[] = [];
  // Server
  address = "";
  addressError = "";
  checking = false;
  problem: ConnectProblem | null = null;
  activating = "";
  private discoverGeneration = 0;
  // Review
  discovery: Discovery | null = null;
  providerId = "";
  name = "";
  nameError = "";
  trusted = false;
  trustError = "";
  saving = false;
  saveProblem: ConnectProblem | null = null;
  fileMode = false;
  private fileReturn: WizardStep = "server";
  fileName = "";
  fileError = "";
  fileLogin: Record<string, unknown> | null = null;
  dragging = false;
  private fileGeneration = 0;
  /** Name of the platform this wizard saved; going back never saves it twice. */
  created = "";
  /** What the saved platform came from: `server:ORIGIN` or `file:TARGET`. */
  private createdVia = "";
  /** The reviewed sign-in option of the platform this wizard created, if any. */
  private createdOption: SignInChoice | null = null;
  removing = false;
  /** What removing the saved platform did, shown on the server step. */
  removedNote = "";
  /** Name of the platform this wizard activated or created. */
  private activeName = "";
  // Sign-in
  login: LoginStatus | null = null;
  outcome: (LoginOutcome & { code: string }) | null = null;
  loginFlow: SignInFlow = "browser";
  starting = false;
  cancelling = false;
  switching = false;
  /** Methods the host or the identity provider refused during this wizard run. */
  private refused = new Set<SignInFlow>();
  copyNote = "";
  openNote = "";
  private loginGeneration = 0;
  private loginWatcher!: LoginWatcher;
  private ownsWatcher = false;
  private destroyed = false;
  private unsubscribe: (() => void) | null = null;
  /** "About N minutes left" from the host's expiry; empty when it states none. */
  remaining = "";
  // Workspace
  workspaceState: WorkspaceState = "loading";
  workspaces: WorkspaceOption[] = [];
  identity: Identity | null = null;
  truncated = false;
  workspaceRevoked = false;
  selectedKey = "";
  workspaceError = "";
  workspaceProblem: ConnectProblem | null = null;
  workspaceSaveProblem: ConnectProblem | null = null;
  selecting = false;
  account: AccountHint | null = null;
  private testGeneration = 0;

  host = hostOf;
  savedServer = platformServer;
  stateLabel = stateLabel;
  problemText = problemText;

  ngOnInit() {
    this.address = this.server();
    this.switching = this.switchAccount();
    this.activeName = this.platform();
    this.loginWatcher = this.watcher() ?? new LoginWatcher(this.client());
    this.ownsWatcher = !this.watcher();
    this.unsubscribe = this.loginWatcher.subscribe((event) =>
      this.loginEvent(event),
    );
    const start = this.start();
    this.step = start;
    this.attend();
    if (start === "sign-in") this.resumeLogin();
    if (start === "workspace") void this.loadWorkspaces();
    this.focusHeading();
  }
  ngOnDestroy() {
    // A pending sign-in keeps running in the host, and the shell's watcher
    // keeps following it; reopening the wizard shows it again.
    this.destroyed = true;
    this.loginGeneration++;
    this.discoverGeneration++;
    this.testGeneration++;
    this.fileGeneration++;
    this.unsubscribe?.();
    if (this.loginWatcher.attendant === this)
      this.loginWatcher.attendant = null;
    if (this.ownsWatcher) this.loginWatcher.stop();
  }
  /** The sign-in step shows the watched sign-in; other steps leave it to the shell. */
  private attend() {
    if (this.step === "sign-in") this.loginWatcher.attendant = this;
    else if (this.loginWatcher.attendant === this)
      this.loginWatcher.attendant = null;
  }

  get title() {
    switch (this.step) {
      case "choice":
        return "How do you want to work?";
      case "server":
        return "Connect to a platform";
      case "review":
        return this.fileMode ? "Use a connection file" : "Review and trust";
      case "sign-in":
        return `Sign in to ${this.platformLabel}`;
      case "workspace":
        return "Choose a workspace";
    }
  }
  get primaryLabel() {
    if (this.step === "server" || this.step === "review") return "Continue";
    if (this.step === "workspace" && this.workspaceState === "ready")
      return "Start working";
    return "";
  }
  get primaryBusy() {
    return (
      this.checking ||
      this.saving ||
      this.selecting ||
      this.removing ||
      !!this.activating
    );
  }
  completed(id: WizardStep) {
    const order = progress.map((p) => p.id);
    return order.indexOf(id) < order.indexOf(this.step);
  }
  get savedPlatforms() {
    return this.status()?.store?.available
      ? (this.status()?.profiles ?? [])
      : [];
  }
  platformState(platform: { name: string }) {
    return platformState(platform, this.status());
  }
  get option(): SignInChoice | null {
    return (
      this.discovery?.sign_in.find((o) => o.provider_id === this.providerId) ??
      null
    );
  }
  get existingName() {
    const name = existingProfileName(this.discovery?.existing_profile);
    return name && name !== this.created ? name : "";
  }
  get platformLabel() {
    return (
      this.activeName ||
      this.status()?.profile?.name ||
      this.discovery?.display_name ||
      hostOf(this.discovery?.server) ||
      "your platform"
    );
  }
  /** The reviewed option, while the active platform is the one created here. */
  private get reviewed() {
    return this.created && this.activeName === this.created
      ? this.createdOption
      : null;
  }
  get providerLabel() {
    return (
      this.reviewed?.display_name ?? this.status()?.profile?.provider_name ?? ""
    );
  }
  get issuerLabel() {
    if (this.reviewed) return this.issuerHost(this.reviewed);
    const profile = this.status()?.profile;
    return hostOf(profile?.issuer_origin || platformIssuer(profile));
  }
  /**
   * Sign-in methods to offer: the administrator's allowed flows for the
   * platform (and, for one reviewed here, what its provider supports), minus
   * any the host or provider refused since.
   */
  get signInFlows(): SignInFlow[] {
    const allowed = this.status()?.profile?.flows ?? null;
    const reviewed = this.reviewed
      ? signInMethods(this.reviewed).filter(
          (flow) => !allowed || allowed.includes(flow),
        )
      : allowed;
    return allowedFlows(reviewed, this.refused);
  }
  get browserAllowed() {
    return this.signInFlows.includes("browser");
  }
  get deviceAllowed() {
    return this.signInFlows.includes("device");
  }
  /** The method "Try again" uses: the last one while still allowed. */
  get retryFlow(): SignInFlow | null {
    const flows = this.signInFlows;
    return flows.includes(this.loginFlow) ? this.loginFlow : (flows[0] ?? null);
  }
  /**
   * The finished sign-in's explanation. With no allowed method left, its
   * advice to try another method is replaced by the one thing left to do.
   */
  get signInProblem() {
    const outcome = this.outcome;
    if (!outcome || this.retryFlow) return outcome;
    return {
      ...outcome,
      hint: `${this.platformLabel} allows no other sign-in method Studio can use. Ask your administrator to check the platform's sign-in settings.`,
    };
  }
  /** The host's guidance for this sign-in, for example switching account with a code. */
  get loginNotice() {
    const notice = this.login?.notice;
    return typeof notice === "string" && notice.length <= 600 ? notice : "";
  }
  /** True on Review for the platform this wizard already saved. */
  get savedHere() {
    if (!this.created) return false;
    if (this.fileMode)
      return (
        !!this.fileLogin &&
        this.createdVia === `file:${this.str(this.fileLogin["target"])}` &&
        this.status()?.store?.available === true
      );
    return (
      !!this.discovery && this.createdVia === `server:${this.discovery.server}`
    );
  }
  get loginUnsupported() {
    return this.status()?.login_supported === false;
  }
  get waiting() {
    return pending(this.login?.state);
  }
  get signInUrl() {
    return safeSignInUrl(
      this.login?.authorization_uri ?? this.login?.verification_uri,
    );
  }
  get deviceCode() {
    const code = this.login?.user_code;
    return typeof code === "string" && code.length <= 64 ? code : "";
  }
  get accessDetails() {
    const profile = this.status()?.profile;
    return accessRequestText({
      platform: this.platformLabel,
      server: platformServer(profile) || this.discovery?.server || "",
      provider: profile?.provider_name || this.option?.display_name,
      issuer: platformIssuer(profile) || this.option?.issuer,
      account:
        this.account ??
        profile?.account ??
        this.status()?.authentication?.account ??
        null,
    });
  }
  issuerHost(option: SignInChoice) {
    return hostOf(option.issuer_origin || option.issuer);
  }
  methodsText(option: SignInChoice) {
    const methods = signInMethods(option);
    if (!methods.length) return "No sign-in method is available";
    const text = methods
      .map((m) =>
        m === "browser" ? "your web browser" : "a code on another device",
      )
      .join(" or ");
    return text.charAt(0).toUpperCase() + text.slice(1);
  }
  problemCode(problem: SignInChoice["problem"]) {
    return problem && typeof problem === "object" ? (problem.code ?? "") : "";
  }
  ids(...values: (string | false | null | undefined)[]) {
    return (
      values
        .filter((v): v is string => typeof v === "string" && !!v)
        .join(" ") || null
    );
  }
  text(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  checked(event: Event) {
    return (event.target as HTMLInputElement).checked;
  }
  str(value: unknown) {
    return typeof value === "string" ? value : "";
  }
  list(value: unknown) {
    return Array.isArray(value) ? value.join(" ") : "";
  }

  // --- navigation ----------------------------------------------------------
  go(step: WizardStep) {
    this.history.push(this.step);
    this.show(step);
  }
  private show(step: WizardStep) {
    this.step = step;
    this.attend();
    this.copyNote = "";
    if (step !== "server") this.removedNote = "";
    // Arriving at sign-in again starts from the choice of method.
    if (step === "sign-in" && !this.waiting) this.outcome = null;
    this.focusHeading();
    this.cdr.markForCheck();
  }
  async back() {
    if (this.step === "sign-in" && this.waiting) await this.cancelSignIn();
    if (this.checking) this.cancelCheck();
    if (this.step === "review" && this.fileMode && this.discovery) {
      this.fileMode = false;
      this.saveProblem = null;
      if (this.fileReturn === "review") {
        this.show("review");
        return;
      }
    }
    const previous = this.history.pop();
    if (!previous) {
      this.cancel.emit();
      return;
    }
    this.fileMode = this.fileMode && previous === "review";
    this.show(previous);
  }
  /** Enter on a checkbox or radio submits the step like it does in text fields. */
  enterKey(event: Event) {
    const target = event.target;
    if (
      target instanceof HTMLInputElement &&
      (target.type === "checkbox" || target.type === "radio")
    )
      this.submit(event);
  }
  submit(event: Event) {
    event.preventDefault();
    switch (this.step) {
      case "server":
        void this.discover();
        break;
      case "review":
        void (this.fileMode ? this.saveFile() : this.saveReview());
        break;
      case "sign-in": {
        if (this.loginUnsupported) {
          void this.checkCli();
          break;
        }
        const flow = this.outcome ? this.retryFlow : this.signInFlows[0];
        if (!this.waiting && !this.starting && flow) void this.startLogin(flow);
        break;
      }
      case "workspace":
        if (this.workspaceState === "ready") void this.chooseWorkspace();
        break;
    }
  }
  private focusHeading() {
    afterNextRender(() => this.heading()?.nativeElement.focus(), {
      injector: this.injector,
    });
  }
  private focusField(id: string) {
    afterNextRender(() => document.getElementById(id)?.focus(), {
      injector: this.injector,
    });
    this.cdr.markForCheck();
  }

  // --- server --------------------------------------------------------------
  async discover() {
    if (this.checking || this.activating) return;
    this.problem = null;
    this.removedNote = "";
    this.addressError = serverAddressError(this.address);
    if (this.addressError) {
      this.focusField("wizard-server-address");
      return;
    }
    const generation = ++this.discoverGeneration;
    this.checking = true;
    try {
      const discovery = await this.client().discover(this.address.trim());
      if (generation !== this.discoverGeneration) return;
      if (!discovery?.sign_in?.length) {
        this.problem = connectProblem("WV-CONNECT-NO-SIGN-IN", this.address);
        return;
      }
      this.discovery = discovery;
      this.address = discovery.server || this.address.trim();
      const usable =
        discovery.sign_in.find((o) => !o.problem && signInMethods(o).length) ??
        discovery.sign_in[0];
      // The server this wizard already saved: show what it was saved with.
      const sameServer =
        !!this.created && this.createdVia === `server:${discovery.server}`;
      const saved = sameServer
        ? discovery.sign_in.find(
            (o) => o.provider_id === this.createdOption?.provider_id,
          )
        : undefined;
      this.providerId = (saved ?? usable).provider_id;
      this.name = sameServer ? this.created : this.suggestedName(discovery);
      this.nameError = "";
      this.trusted = false;
      this.trustError = "";
      this.saveProblem = null;
      this.fileMode = false;
      this.go("review");
    } catch (error) {
      if (generation !== this.discoverGeneration) return;
      const plain = describeError(error);
      if (plain.code === "WV-STUDIO-SESSION") return;
      const body = error instanceof ApiError ? record(error.detail) : {};
      this.problem = connectProblem(
        plain.code,
        this.address,
        body["suggested_server"] ?? body["detail"],
        plain.message,
      );
      if (!plain.code.startsWith("WV-CONNECT")) {
        this.problem.hint = plain.message;
        this.problem.title = "Studio couldn't check this server.";
      }
    } finally {
      if (generation === this.discoverGeneration) this.checking = false;
      this.cdr.markForCheck();
    }
  }
  cancelCheck() {
    this.discoverGeneration++;
    this.checking = false;
    this.focusField("wizard-server-address");
  }
  private suggestedName(discovery: Discovery) {
    const suggested = discovery.suggested_name ?? "";
    if (!profileNameError(suggested)) return suggested;
    const base = (discovery.display_name || hostOf(discovery.server))
      .normalize("NFKD")
      .replace(/[^A-Za-z0-9 ._-]+/g, " ")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/^[ ._-]+/, "")
      .slice(0, 64)
      .trim();
    return profileNameError(base) ? "Platform" : base;
  }
  problemAction(action: NonNullable<ConnectProblem["action"]>) {
    if (action.kind === "connection-file") {
      this.openFile();
      return;
    }
    if (action.kind === "address" && action.value) {
      if (this.step !== "server") {
        const index = this.history.indexOf("server");
        if (index >= 0) this.history = this.history.slice(0, index);
        this.fileMode = false;
        this.show("server");
      }
      this.address = action.value;
      this.problem = null;
      void this.discover();
    }
  }
  async useSaved(name: string) {
    if (this.activating) return;
    const status = this.status();
    if (status?.profile?.name === name && status.profile.saved !== false) {
      this.activeName = name;
      this.afterActivation(status);
      return;
    }
    this.activating = name;
    if (!(await this.confirmSwitch()("platform"))) {
      this.activating = "";
      this.cdr.markForCheck();
      return;
    }
    this.problem = null;
    this.saveProblem = null;
    this.cdr.markForCheck();
    try {
      const result = await this.client().activate(name);
      this.activeName = name;
      this.switched.emit(result);
      this.afterActivation(result.connection);
    } catch (error) {
      const plain = describeError(error);
      if (plain.code === "WV-STUDIO-SESSION") return;
      const target: ConnectProblem = {
        title:
          plain.code === "WV-PROFILE-CHANGED"
            ? `The sign-in settings for ${name} changed on the server.`
            : `Studio couldn't open ${name}.`,
        hint:
          plain.code === "WV-PROFILE-CHANGED"
            ? "Enter its address to review the new settings."
            : plain.message,
        code: plain.code,
      };
      if (this.step === "review") this.saveProblem = target;
      else this.problem = target;
    } finally {
      this.activating = "";
      this.cdr.markForCheck();
    }
  }
  private afterActivation(connection: ConnectionStatus | null | undefined) {
    // An expired access token with a refresh token is renewed by the check.
    const signedIn = sessionUsable(connection?.authentication);
    if (
      this.loginWatcher.pending ||
      (connection?.login && pending(connection.login.state))
    ) {
      this.go("sign-in");
      this.resumeLogin(connection?.login);
    } else if (signedIn && !this.switching) {
      this.go("workspace");
      void this.loadWorkspaces();
    } else this.go("sign-in");
  }

  // --- review --------------------------------------------------------------
  chooseProvider(option: SignInChoice) {
    this.providerId = option.provider_id;
    this.trusted = false;
    this.saveProblem = null;
  }
  async saveReview() {
    if (this.saving || this.removing || !this.discovery) return;
    // Saved earlier in this run: continue without saving a second platform.
    if (this.savedHere) {
      this.go("sign-in");
      return;
    }
    const option = this.option;
    this.saveProblem = null;
    this.nameError = profileNameError(this.name);
    this.trustError = this.trusted
      ? ""
      : "Confirm that you trust this server and identity provider to continue.";
    if (!option || option.problem || !signInMethods(option).length) {
      this.saveProblem = {
        title: "Choose an identity provider you can use.",
        hint: "None of the sign-in options from this server can be used right now. Ask your administrator to check the platform's sign-in settings.",
        code: this.problemCode(option?.problem),
      };
      return;
    }
    if (this.nameError) return this.focusField("wizard-name");
    if (this.trustError) return this.focusField("wizard-trust");
    this.saving = true;
    if (!(await this.confirmSwitch()("platform"))) {
      this.saving = false;
      this.cdr.markForCheck();
      return;
    }
    this.cdr.markForCheck();
    try {
      const result = await this.client().createProfile({
        name: this.name,
        server: this.discovery.server,
        provider_id: option.provider_id,
        issuer: option.issuer,
        client_id: option.client_id,
        trust_confirmed: true,
      });
      this.created = this.activeName = this.name;
      this.createdVia = `server:${this.discovery.server}`;
      this.createdOption = option;
      this.switched.emit(result);
      this.afterActivation(result.connection);
    } catch (error) {
      this.reviewFailure(error);
    } finally {
      this.saving = false;
      this.cdr.markForCheck();
    }
  }
  private reviewFailure(error: unknown) {
    const plain = describeError(error);
    if (plain.code === "WV-STUDIO-SESSION") return;
    if (plain.code === "WV-PROFILE-EXISTS") {
      this.nameError = `You already have a platform named ${this.name}. Choose another name.`;
      this.focusField("wizard-name");
    } else if (plain.code === "WV-PROFILE-NAME") {
      this.nameError =
        profileNameError(this.name) ||
        "Choose a different name. Start with a letter or digit.";
      this.focusField("wizard-name");
    } else if (plain.code === "WV-PROFILE-CHANGED") {
      this.saveProblem = {
        ...connectProblem(plain.code, this.address),
        action: {
          kind: "address",
          label: "Review again",
          value: this.discovery?.server ?? this.address,
        },
      };
    } else if (plain.code.startsWith("WV-CONNECT")) {
      const body = error instanceof ApiError ? record(error.detail) : {};
      this.saveProblem = connectProblem(
        plain.code,
        this.address,
        body["suggested_server"],
        plain.message,
      );
    } else
      this.saveProblem = {
        title: "Studio couldn't save this platform.",
        hint: plain.message,
        code: plain.code,
      };
  }
  openFile() {
    this.fileReturn = this.step === "review" ? "review" : "server";
    this.fileMode = true;
    this.saveProblem = null;
    this.trustError = "";
    this.nameError = "";
    this.trusted = false;
    if (!this.fileLogin) this.name = "";
    if (this.step === "review") this.show("review");
    else this.go("review");
  }
  async chooseFile(event: Event) {
    const picker = event.target as HTMLInputElement;
    const file = picker.files?.[0];
    picker.value = "";
    if (file) await this.readFile(file);
  }
  async dropFile(event: DragEvent) {
    event.preventDefault();
    this.dragging = false;
    const file = event.dataTransfer?.files[0];
    if (file) await this.readFile(file);
  }
  private async readFile(file: File) {
    if (this.saving) return;
    const generation = ++this.fileGeneration;
    this.fileName = file.name;
    this.fileLogin = null;
    this.fileError = "";
    this.trusted = false;
    this.saveProblem = null;
    try {
      if (file.size > 65536)
        throw Error("Connection files are limited to 64 KiB.");
      const content = await file.text();
      if (generation !== this.fileGeneration) return;
      const parsed = parseConnectionFile(content, file.name);
      this.fileLogin = parsed.login;
      this.name = profileNameError(parsed.name)
        ? this.suggestedName({
            server: this.str(parsed.login["target"]),
            display_name: parsed.name,
            sign_in: [],
          })
        : parsed.name;
    } catch (error) {
      if (generation !== this.fileGeneration) return;
      this.fileError =
        error instanceof Error
          ? error.message
          : "Studio couldn't read this connection file.";
    } finally {
      this.cdr.markForCheck();
    }
  }
  async saveFile() {
    if (this.saving || this.removing) return;
    if (this.savedHere) {
      this.go("sign-in");
      return;
    }
    this.saveProblem = null;
    if (!this.fileLogin) {
      this.fileError = this.fileError || "Choose a connection file first.";
      return this.focusField("wizard-file-button");
    }
    this.nameError = profileNameError(this.name);
    this.trustError = this.trusted
      ? ""
      : "Confirm that you trust this server and identity provider to continue.";
    if (this.nameError) return this.focusField("wizard-name");
    if (this.trustError) return this.focusField("wizard-trust");
    this.saving = true;
    if (!(await this.confirmSwitch()("platform"))) {
      this.saving = false;
      this.cdr.markForCheck();
      return;
    }
    this.cdr.markForCheck();
    try {
      const login: Record<string, unknown> = {
        ...this.fileLogin,
        account: this.str(this.fileLogin["account"]) || this.name,
      };
      const session = await this.client().configure(this.name, login);
      const status = await this.client()
        .status()
        .catch(() => null);
      const connection: ConnectionStatus = isConnectionStatus(status)
        ? status
        : { configured: true, login_supported: true };
      this.created = this.activeName = this.name;
      this.createdVia = `file:${this.str(login["target"])}`;
      this.createdOption = null;
      this.switched.emit({ session, connection });
      this.afterActivation(connection);
    } catch (error) {
      const plain = describeError(error);
      if (plain.code === "WV-STUDIO-SESSION") return;
      if (
        plain.code === "WV-PROFILE-EXISTS" ||
        plain.code === "WV-PROFILE-NAME"
      )
        this.reviewFailure(error);
      else
        this.saveProblem = {
          title: "Studio couldn't use this connection file.",
          hint: plain.message,
          code: plain.code,
        };
    } finally {
      this.saving = false;
      this.cdr.markForCheck();
    }
  }

  /**
   * Forgets the platform saved in this run (signing it out on this computer)
   * and returns to the server step with the address kept.
   */
  async removeAndStartOver() {
    if (this.removing || !this.created) return;
    const name = this.created;
    this.removing = true;
    this.saveProblem = null;
    this.cdr.markForCheck();
    try {
      const result = await this.client().remove(name, true);
      this.switched.emit(result);
      this.startOver();
      this.removedNote = this.removalText(name, result);
    } catch (error) {
      const plain = describeError(error);
      if (plain.code === "WV-STUDIO-SESSION") return;
      // Already gone (for example removed in a terminal): start over anyway.
      if (plain.code === "WV-PROFILE-NOT-FOUND") this.startOver();
      else
        this.saveProblem = {
          title: `Studio couldn't remove ${name}.`,
          hint: plain.message,
          code: plain.code,
        };
    } finally {
      this.removing = false;
      this.cdr.markForCheck();
    }
  }
  /** What happened to the removed platform's sign-in, in one sentence or two. */
  private removalText(name: string, result: unknown) {
    switch (signOutReport(result)) {
      case "incomplete":
        return `Removed ${name}, but Studio couldn't remove its sign-in from this computer's credential store.`;
      case "unconfirmed":
        return `Removed ${name}. ${revocationNote}`;
      default:
        return `Removed ${name}.`;
    }
  }
  private startOver() {
    if (this.discovery?.server) this.address = this.discovery.server;
    else if (this.fileMode && this.fileLogin)
      this.address = this.str(this.fileLogin["target"]) || this.address;
    this.created = this.createdVia = this.activeName = "";
    this.createdOption = null;
    this.discovery = null;
    this.fileMode = false;
    this.fileLogin = null;
    this.fileName = "";
    this.trusted = false;
    this.refused.clear();
    this.login = null;
    this.outcome = null;
    const index = this.history.indexOf("server");
    this.history = index >= 0 ? this.history.slice(0, index) : [];
    this.show("server");
  }

  // --- sign-in -------------------------------------------------------------
  /** Shows the sign-in the watcher follows, or follows the host's pending one. */
  private resumeLogin(login = this.status()?.login) {
    if (this.loginWatcher.pending && this.loginWatcher.login) {
      this.mirror(this.loginWatcher.login);
      return;
    }
    if (!login?.id || !pending(login.state)) return;
    this.loginGeneration++;
    this.loginWatcher.watch(login);
  }
  async startLogin(flow: SignInFlow) {
    if (this.starting) return;
    const generation = ++this.loginGeneration;
    this.loginWatcher.stop();
    this.loginFlow = flow;
    this.outcome = null;
    this.openNote = "";
    this.copyNote = "";
    this.remaining = "";
    this.starting = true;
    this.login = { state: "starting", flow };
    this.cdr.markForCheck();
    try {
      const login = await this.client().startLogin(flow, this.switching);
      if (generation !== this.loginGeneration) {
        // Canceled while starting: stop the flow the host just began.
        if (this.cancelledGeneration === generation && login?.id)
          void this.client()
            .cancelLogin(login.id)
            .catch(() => undefined);
        // Closed while starting: the shell's watcher follows it from here.
        else if (this.destroyed && !this.ownsWatcher)
          this.loginWatcher.watch(login);
        return;
      }
      this.loginWatcher.watch(login);
    } catch (error) {
      if (generation !== this.loginGeneration) return;
      const plain = describeError(error);
      if (plain.code === "WV-STUDIO-SESSION") return;
      this.login = null;
      // The host refuses a method the administrator doesn't allow.
      if (plain.code === "WV-AUTH-FLOW") this.refused.add(flow);
      this.outcome = plain.code.startsWith("WV-AUTH")
        ? { ...loginOutcome("failed", plain.code), code: plain.code }
        : {
            title: "Sign-in didn't start",
            hint: plain.message,
            code: plain.code,
          };
    } finally {
      this.starting = false;
      this.cdr.markForCheck();
    }
  }
  private cancelledGeneration = -1;
  private mirror(login: LoginStatus) {
    this.login = login;
    this.loginFlow = login.flow === "device" ? "device" : "browser";
    this.remaining = this.loginWatcher.remaining;
  }
  /** Sign-in progress from the watcher; only the sign-in step reacts to it. */
  private loginEvent(event: LoginEvent) {
    if (this.step !== "sign-in") return;
    switch (event.kind) {
      case "waiting":
        this.mirror(event.login);
        break;
      case "authenticated":
        this.login = null;
        void this.signedIn(this.loginGeneration);
        break;
      case "ended":
        this.login = null;
        if (event.code === "WV-AUTH-DEVICE-UNAVAILABLE")
          // Try again with the browser; the code option is hidden from now on.
          this.refused.add("device");
        if (event.code === "WV-AUTH-FLOW") this.refused.add(this.loginFlow);
        this.outcome = {
          ...loginOutcome(event.state, event.code),
          code:
            event.state === "cancelled" || event.state === "timeout"
              ? ""
              : event.code,
        };
        break;
      case "stopped":
        this.login = null;
        break;
    }
    this.cdr.markForCheck();
  }
  async cancelSignIn() {
    const id = this.loginWatcher.login?.id ?? this.login?.id ?? "";
    this.cancelledGeneration = ++this.loginGeneration;
    this.loginWatcher.stop();
    this.cancelling = true;
    this.cdr.markForCheck();
    try {
      if (id) await this.client().cancelLogin(id);
    } catch {
      // The flow may already have finished; the next attempt replaces it.
    } finally {
      this.cancelling = false;
      this.starting = false;
      this.login = null;
      this.outcome = { ...loginOutcome("cancelled"), code: "" };
      this.cdr.markForCheck();
    }
  }
  async openAgain() {
    const id = this.login?.id;
    if (!id) return;
    this.openNote = "";
    try {
      const result = record(await this.client().openLogin(id));
      if (result["opened"] === false)
        this.openNote =
          "Studio couldn't open your web browser. Copy the address and paste it into your browser.";
    } catch (error) {
      if (describeError(error).code === "WV-STUDIO-SESSION") return;
      this.openNote =
        "Studio couldn't open your web browser. Copy the address and paste it into your browser.";
    } finally {
      this.cdr.markForCheck();
    }
  }
  private async signedIn(generation: number) {
    const status = await this.client()
      .status()
      .catch(() => null);
    if (generation !== this.loginGeneration) return;
    if (isConnectionStatus(status)) this.statusChange.emit(status);
    this.switching = false;
    this.outcome = null;
    this.go("workspace");
    void this.loadWorkspaces();
  }
  signInAgain(switchAccount: boolean) {
    this.switching = switchAccount;
    this.outcome = null;
    this.login = null;
    this.go("sign-in");
  }
  private async checkCli() {
    this.go("workspace");
    await this.loadWorkspaces();
  }
  async copy(value: string, id: string) {
    const copied = await copyText(value);
    if (copied) this.copyNote = "Copied.";
    else {
      const element = document.getElementById(id);
      if (
        element instanceof HTMLInputElement ||
        element instanceof HTMLTextAreaElement
      ) {
        element.focus();
        element.select();
      } else if (element) {
        const range = document.createRange();
        range.selectNodeContents(element);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
      }
      this.copyNote =
        "Copying isn't available here. The text is selected: press Ctrl+C (or ⌘C on a Mac) to copy it.";
    }
    this.cdr.markForCheck();
  }

  // --- workspace -----------------------------------------------------------
  async loadWorkspaces() {
    const generation = ++this.testGeneration;
    this.workspaceState = "loading";
    this.workspaceError = "";
    this.workspaceProblem = null;
    this.workspaceSaveProblem = null;
    this.account = null;
    this.cdr.markForCheck();
    try {
      const result = await this.client().test();
      if (generation !== this.testGeneration) return;
      if (!result?.identity)
        throw Error("Studio couldn't read your account from the platform.");
      this.identity = result.identity;
      this.workspaces = Array.isArray(result.workspaces)
        ? result.workspaces
        : optionsFromIdentity(result.identity);
      this.truncated = !!(result.truncated ?? result.identity.truncated);
      this.workspaceRevoked = !!result.workspace_revoked;
      this.verified.emit({
        session: result.session ?? null,
        identity: result.identity,
        workspaces: this.workspaces,
      });
      const current = this.workspaceRevoked ? null : this.currentKey();
      this.selectedKey = this.workspaces.some(
        (o) => workspaceKey(o) === current,
      )
        ? (current ?? "")
        : this.workspaces.length === 1
          ? workspaceKey(this.workspaces[0])
          : "";
      this.workspaceState = this.workspaces.length ? "ready" : "empty";
    } catch (error) {
      if (generation !== this.testGeneration) return;
      const plain = describeError(error);
      if (plain.code === "WV-STUDIO-SESSION") return;
      const body = error instanceof ApiError ? record(error.detail) : {};
      if (plain.code === "WV-AUTH-NOT-LINKED") {
        this.workspaceState = "not-linked";
        // The subject is null when the identity provider shared none.
        this.account = accountHint(body["account"]);
        this.accountProblem.emit("not-linked");
      } else if (plain.status === 401) {
        this.workspaceState = "expired";
        this.accountProblem.emit("expired");
      } else {
        this.workspaceState = "error";
        this.workspaceProblem = {
          title: "Studio couldn't load your workspaces.",
          hint: plain.message,
          code: plain.code,
        };
      }
    } finally {
      this.cdr.markForCheck();
    }
  }
  private currentKey() {
    const current = this.currentWorkspace();
    if (current?.tenantId && current.projectId && current.environmentId)
      return `${current.tenantId}/${current.projectId}/${current.environmentId}`;
    const saved = this.status()?.profile?.workspace;
    return saved ? workspaceKey(saved) : null;
  }
  pickWorkspace(option: WorkspaceOption) {
    this.selectedKey = workspaceKey(option);
    this.workspaceError = "";
    this.workspaceSaveProblem = null;
  }
  async chooseWorkspace() {
    if (this.selecting) return;
    const option = this.workspaces.find(
      (o) => workspaceKey(o) === this.selectedKey,
    );
    if (!option) {
      this.workspaceError = "Choose a workspace to continue.";
      afterNextRender(
        () =>
          document
            .querySelector<HTMLInputElement>(
              "weave-workspace-picker input[type=radio]",
            )
            ?.focus(),
        { injector: this.injector },
      );
      this.cdr.markForCheck();
      return;
    }
    const current = this.currentWorkspace();
    if (
      current?.environmentId &&
      workspaceKey(option) ===
        `${current.tenantId}/${current.projectId}/${current.environmentId}`
    ) {
      this.finish.emit();
      return;
    }
    this.selecting = true;
    if (!(await this.confirmSwitch()("workspace"))) {
      this.selecting = false;
      this.cdr.markForCheck();
      return;
    }
    this.workspaceSaveProblem = null;
    this.cdr.markForCheck();
    try {
      const session = await this.client().selectWorkspace(option);
      this.workspaceChange.emit({ session, identity: this.identity });
      this.finish.emit();
    } catch (error) {
      const plain = describeError(error);
      if (plain.code === "WV-STUDIO-SESSION") return;
      this.workspaceSaveProblem = {
        title: "Studio couldn't open this workspace.",
        hint: plain.message,
        code: plain.code,
      };
    } finally {
      this.selecting = false;
      this.cdr.markForCheck();
    }
  }
}
