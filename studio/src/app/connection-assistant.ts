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
import {
  Component,
  Input,
  Output,
  EventEmitter,
  inject,
  ChangeDetectorRef,
  OnChanges,
  SimpleChanges,
} from "@angular/core";
export interface ConnectionConfiguration {
  name: string;
  login: Record<string, unknown>;
  trust_confirmed: true;
}
@Component({
  selector: "weave-connection-assistant",
  standalone: true,
  template: `<section class="connection-assistant settings-card">
    <h2>Connect a workspace</h2>
    <p>
      Import the non-secret login configuration from your operator, review the
      API and identity provider, then sign in. Credentials stay in the Python
      host’s credential store.
    </p>
    <ol class="connection-progress" aria-label="Connection stages">
      <li [class.complete]="!configurationDirty && status?.['configured']">
        1 · Review connection
      </li>
      <li
        [class.complete]="
          !configurationDirty && authentication['authenticated']
        "
      >
        2 · Sign in
      </li>
      <li [class.complete]="workspaceSelected">3 · Choose workspace</li>
    </ol>
    <fieldset [disabled]="busy" class="configuration-controls">
      <div
        class="configuration-dropzone"
        (dragover)="$event.preventDefault()"
        (drop)="dropConfiguration($event)"
      >
        <p class="import-caption">
          Operator login configuration · JSON · up to 64 KiB · no secrets
        </p>
        <button class="button" type="button" (click)="configPicker.click()">
          {{
            filename
              ? "Replace login configuration"
              : "Import login configuration"
          }}</button
        ><input
          #configPicker
          type="file"
          accept=".json"
          aria-label="Import login configuration"
          (change)="importConfiguration($event)"
          hidden
        />
        <p class="import-caption">
          {{
            filename
              ? filename +
                (error ? " · Needs attention" : " · Imported for review")
              : "Choose a file or drop it here"
          }}
        </p>
      </div>
      <details>
        <summary>Enter connection details</summary>
        <div class="connection-fields">
          <label
            >Connection name<input
              [value]="name"
              (input)="name = text($event); configurationDirty = true"
          /></label>
          <label
            >Provider ID<input
              [value]="login['provider_id'] || ''"
              (input)="set('provider_id', text($event))"
          /></label>
          <label
            >Issuer URL<input
              type="url"
              [value]="login['issuer'] || ''"
              (input)="set('issuer', text($event))"
          /></label>
          <label
            >Client ID<input
              [value]="login['client_id'] || ''"
              (input)="set('client_id', text($event))"
          /></label>
          <label
            >API origin<input
              type="url"
              [value]="login['target'] || ''"
              (input)="set('target', text($event))"
          /></label>
          <label
            >Credential account<input
              [value]="login['account'] || ''"
              (input)="set('account', text($event))"
          /></label>
        </div>
        <details>
          <summary>Advanced trust options</summary>
          <label
            >OAuth scopes (space separated)<input
              [value]="list('scopes')"
              (input)="set('scopes', words($event))" /></label
          ><label
            >Trusted endpoint origins (space separated)<input
              [value]="list('trusted_endpoint_origins')"
              (input)="set('trusted_endpoint_origins', words($event))" /></label
          ><label class="checkbox-field"
            ><input
              type="checkbox"
              [checked]="login['allow_loopback_http'] === true"
              (change)="set('allow_loopback_http', checked($event))"
            />Allow HTTP for loopback only</label
          >
        </details>
      </details>
      @if (ready) {
        <div class="connection-review">
          <h3>Review the connection</h3>
          <dl>
            <dt>API target</dt>
            <dd>{{ login["target"] }}</dd>
            <dt>Identity provider</dt>
            <dd>{{ login["provider_id"] }} · {{ login["issuer"] }}</dd>
            <dt>Client / account</dt>
            <dd>{{ login["client_id"] }} / {{ login["account"] }}</dd>
            <dt>OAuth scopes</dt>
            <dd>{{ list("scopes") }}</dd>
            <dt>Trusted endpoints</dt>
            <dd>
              {{ list("trusted_endpoint_origins") || "Issuer origin only" }}
            </dd>
          </dl>
          <label class="checkbox-field"
            ><input
              type="checkbox"
              [checked]="trusted"
              (change)="trusted = checked($event)"
            />I trust this API target and identity provider</label
          ><button
            class="primary"
            [disabled]="!trusted || busy"
            (click)="
              configure.emit({
                name: name,
                login: login,
                trust_confirmed: true,
              })
            "
          >
            Save reviewed connection
          </button>
        </div>
      }
      @if (error) {
        <p role="alert" class="error">{{ error }}</p>
      }
    </fieldset>
    @if (status?.["configured"]) {
      <div class="connection-login">
        <h3>Sign in</h3>
        <p>
          Authentication:
          {{
            authentication["authenticated"]
              ? "Signed in"
              : authentication["reauthentication_required"]
                ? "Sign in again"
                : "Not signed in"
          }}
        </p>
        @if (status["login_supported"] === false) {
          <p>
            This profile uses CLI-managed authentication. Sign in with CLI or
            import a reviewed native-store connection.
          </p>
        }
        @if (configurationDirty) {
          <p>
            Save this reviewed connection first. Sign-in and connection checks
            use the saved configuration.
          </p>
        }
        <div class="tool-group">
          <button
            [disabled]="
              busy ||
              pending ||
              configurationDirty ||
              status['login_supported'] === false
            "
            (click)="start.emit()"
          >
            {{
              authentication["authenticated"] ? "Sign in again" : "Sign in"
            }}</button
          ><button
            [disabled]="busy || pending || configurationDirty"
            (click)="check.emit()"
          >
            Check connection and discover workspaces
          </button>
        </div>
        @if (pending) {
          <p role="status">
            Waiting for sign-in. Open this address in your browser and complete
            the identity provider’s instructions.
          </p>
        }
        @if (loginUrl) {
          <p>
            Open this address in your web browser. If this desktop window blocks
            the link, copy the address into your browser.
          </p>
          <a [href]="loginUrl" target="_blank" rel="noopener noreferrer"
            >Open sign-in page</a
          ><label>Sign-in address<input readonly [value]="loginUrl" /></label
          ><button (click)="copy(loginUrl)">Copy sign-in address</button>
        }
        @if (copyFeedback) {
          <p role="status">{{ copyFeedback }}</p>
        }
        @if (loginStatus["user_code"]) {
          <label
            >Device code<input
              readonly
              [value]="loginStatus['user_code']" /></label
          ><button (click)="copy(String(loginStatus['user_code']))">
            Copy device code
          </button>
          <p>
            Enter this code at the sign-in address. If a desktop window blocks
            opening the link, copy the address into your browser.
          </p>
        }
        @if (loginStatus["state"]) {
          <p role="status">Login status: {{ loginStatus["state"] }}</p>
        }
        @if (loginStatus["error_code"]) {
          <p role="alert">
            Sign-in failed: {{ loginStatus["error_code"] }}. Review the
            configuration or select Sign in to try again.
          </p>
        }
        @if (pending) {
          <button
            (click)="cancel.emit(String(loginStatus['id']))"
            [disabled]="busy"
          >
            Cancel sign-in
          </button>
        }
      </div>
    }
  </section>`,
  styles: [
    `
      .configuration-controls {
        border: 0;
        margin: 0;
        padding: 0;
        min-width: 0;
      }
      .configuration-dropzone {
        border: 1px dashed #8caaa0;
        border-radius: 12px;
        background: #f5faf7;
        padding: 20px;
        margin: 20px 0;
      }
      .import-caption {
        color: #526d63;
        font-size: 13px;
      }
      .connection-progress {
        list-style: none;
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        padding: 0;
        margin: 20px 0;
      }
      .connection-progress li {
        border-radius: 20px;
        padding: 8px 12px;
        background: #edf2ef;
        font-size: 12px;
        font-weight: 600;
      }
      .connection-progress .complete {
        background: #dbece3;
        color: #174d3c;
      }
      input[type="file"] {
        display: none;
      }
      .connection-fields {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 12px;
        margin: 16px 0;
      }
      .connection-review,
      .connection-login {
        border-top: 1px solid var(--border);
        margin-top: 20px;
        padding-top: 20px;
      }
      dl {
        display: grid;
        grid-template-columns: 140px 1fr;
        gap: 8px;
      }
      @media (max-width: 600px) {
        dl {
          grid-template-columns: 1fr;
          gap: 4px;
        }
        dd {
          margin-bottom: 12px;
        }
      }
      dd {
        margin: 0;
        overflow-wrap: anywhere;
      }
      details {
        margin: 12px 0;
      }
      .connection-assistant input {
        max-width: 100%;
      }
      .connection-assistant .checkbox-field {
        display: flex;
        gap: 8px;
        align-items: center;
      }
      .checkbox-field input {
        width: auto;
      }
    `,
  ],
})
export class ConnectionAssistant implements OnChanges {
  private cdr = inject(ChangeDetectorRef);
  @Input() status: Record<string, unknown> | null = null;
  @Input() busy = false;
  @Input() savedRevision = 0;
  @Input() workspaceSelected = false;
  configurationDirty = false;
  ngOnChanges(changes: SimpleChanges) {
    if (changes["savedRevision"] && !changes["savedRevision"].firstChange)
      this.configurationDirty = false;
  }
  @Output() configure = new EventEmitter<ConnectionConfiguration>();
  @Output() start = new EventEmitter<void>();
  @Output() check = new EventEmitter<void>();
  @Output() cancel = new EventEmitter<string>();
  String = String;
  name = "My workspace";
  login: Record<string, unknown> = { scopes: ["openid", "profile", "email"] };
  trusted = false;
  error = "";
  filename = "";
  private importGeneration = 0;
  copyFeedback = "";
  get ready() {
    return ["provider_id", "issuer", "client_id", "target", "account"].every(
      (key) =>
        typeof this.login[key] === "string" && String(this.login[key]).trim(),
    );
  }
  get authentication() {
    return (this.status?.["authentication"] ?? {}) as Record<string, unknown>;
  }
  get loginStatus() {
    return (this.status?.["login"] ?? {}) as Record<string, unknown>;
  }
  get pending() {
    return ["starting", "awaiting_user"].includes(
      String(this.loginStatus["state"]),
    );
  }
  get loginUrl() {
    const value =
      this.loginStatus["authorization_uri"] ??
      this.loginStatus["verification_uri"];
    if (typeof value !== "string") return "";
    try {
      const url = new URL(value);
      return url.protocol === "https:" ||
        (url.protocol === "http:" &&
          ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname))
        ? value
        : "";
    } catch {
      return "";
    }
  }
  words(event: Event) {
    return this.text(event).split(/\s+/).filter(Boolean);
  }
  text(event: Event) {
    return (event.target as HTMLInputElement).value;
  }
  checked(event: Event) {
    return (event.target as HTMLInputElement).checked;
  }
  list(key: string) {
    return Array.isArray(this.login[key])
      ? (this.login[key] as string[]).join(" ")
      : "";
  }
  set(key: string, value: unknown) {
    if (this.busy) return;
    this.configurationDirty = true;
    this.login = { ...this.login, [key]: value };
    this.trusted = false;
  }
  async importConfiguration(event: Event) {
    const input = event.target as HTMLInputElement,
      file = input.files?.[0];
    input.value = "";
    if (!file) return;
    await this.readConfiguration(file);
  }
  async dropConfiguration(event: DragEvent) {
    event.preventDefault();
    const file = event.dataTransfer?.files[0];
    if (file) await this.readConfiguration(file);
  }
  async readConfiguration(file: File) {
    if (this.busy) return;
    const generation = ++this.importGeneration;
    this.configurationDirty = true;
    this.trusted = false;
    this.login = {};
    this.filename = file.name;
    try {
      if (file.size > 65536)
        throw Error("Login configuration is limited to 64 KiB.");
      const content = await file.text();
      if (generation !== this.importGeneration) return;
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(content);
      } catch {
        throw Error(
          "The file is not valid JSON. Ask your operator for a non-secret login configuration.",
        );
      }
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
        throw Error("Choose a LoginConfig object.");
      if (
        "login" in parsed &&
        Object.keys(parsed).some((key) => !["name", "login"].includes(key))
      )
        throw Error(
          "Connection files may contain only name and login. Tokens and secrets are not accepted.",
        );
      const login = parsed.login ?? parsed;
      const allowed = [
        "provider_id",
        "issuer",
        "client_id",
        "target",
        "account",
        "scopes",
        "trusted_endpoint_origins",
        "allow_loopback_http",
        "timeout",
        "login_timeout",
        "require_refresh_rotation",
      ];
      if (
        !login ||
        typeof login !== "object" ||
        Array.isArray(login) ||
        Object.keys(login).some((key) => !allowed.includes(key))
      )
        throw Error(
          "Import a non-secret LoginConfig file. Tokens, secrets and credential file paths are not accepted.",
        );
      this.login = {
        scopes: ["openid", "profile", "email"],
        ...structuredClone(login as Record<string, unknown>),
      };
      this.name =
        typeof parsed.name === "string"
          ? parsed.name
          : file.name.replace(/\.json$/i, "");
      this.trusted = false;
      this.error = "";
    } catch (e) {
      if (generation !== this.importGeneration) return;
      this.login = {};
      this.trusted = false;
      this.error =
        e instanceof Error ? e.message : "Invalid login configuration.";
    } finally {
      this.cdr.markForCheck();
    }
  }
  async copy(value: string) {
    try {
      await navigator.clipboard.writeText(value);
      this.copyFeedback = "Copied. Paste it into your web browser.";
      this.error = "";
    } catch {
      this.error = "Select and copy the address or code manually.";
    } finally {
      this.cdr.markForCheck();
    }
  }
}
