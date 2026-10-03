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
  ChangeDetectorRef,
  Component,
  Input,
  OnDestroy,
  OnChanges,
  SimpleChanges,
  inject,
} from "@angular/core";
import type { App } from "../app";
import { describeError } from "../errors";
@Component({
  selector: "weave-worker-controls",
  standalone: true,
  template: `
    <p class="hint">
      Registration is not health. Contact and capacity below are dated server
      observations; they do not establish container readiness.
    </p>
    <dl class="task-metadata">
      <dt>Claim mode</dt>
      <dd>
        {{
          record["revoked"]
            ? "Revoked"
            : record["draining"]
              ? "Draining"
              : record["draining"] === false
                ? "Accepting claims"
                : "Unknown"
        }}
      </dd>
      <dt>Presence</dt>
      <dd>{{ presence }}</dd>
      <dt>Last contact</dt>
      <dd>{{ record["last_seen_at"] || "Unknown" }}</dd>
      <dt>Registered task limit</dt>
      <dd>{{ record["capacity"] ?? "Unknown" }}</dd>
      <dt>Active leases at observation</dt>
      <dd>{{ record["active_leases"] ?? "Unknown" }}</dd>
      <dt>Available capacity at observation</dt>
      <dd>{{ available }}</dd>
      <dt>Observed at</dt>
      <dd>{{ record["observed_at"] || "Unknown" }}</dd>
    </dl>
    <p>
      Draining stops new claims. Existing tasks may finish or expire; draining
      does not stop the container or cancel those tasks.
    </p>
    @if (error) {
      <p role="alert">{{ error }}</p>
    }
    @if (canControl) {
      <button [disabled]="busy" (click)="control()">
        {{ record["draining"] ? "Resume claims" : "Drain worker" }}
      </button>
    }
  `,
  styles: `
    :host {
      display: grid;
      gap: 12px;
    }
    dl {
      margin: 0;
    }
    dt {
      font-weight: 600;
    }
  `,
})
export class WorkerControls implements OnDestroy, OnChanges {
  @Input({ required: true }) host!: App;
  @Input({ required: true }) record!: Record<string, unknown>;
  busy = false;
  error = "";
  private alive = true;
  private cdr = inject(ChangeDetectorRef);
  private timer = setInterval(() => this.cdr.markForCheck(), 5000);
  get presence() {
    if (!this.record["presence"] || this.record["presence"] === "unknown")
      return "Unknown";
    return this.record["presence"] === "recent" &&
      Date.parse(String(this.record["presence_expires_at"])) > Date.now()
      ? "Recent contact"
      : "Contact expired";
  }
  get available() {
    return this.presence === "Recent contact"
      ? (this.record["available_capacity"] ?? "Unknown")
      : "Unknown";
  }
  get canControl() {
    return (
      !this.host.signInEnded &&
      !this.record["revoked"] &&
      Number.isInteger(this.record["revision"]) &&
      this.host.can("worker.drain", String(this.record["id"]))
    );
  }
  async control() {
    if (!this.canControl || this.busy) return;
    const id = String(this.record["id"]);
    const scope = this.host.api.environment;
    const identity = JSON.stringify(this.host.identity);
    const revision = this.record["revision"];
    const action = this.record["draining"] ? "resume" : "drain";
    const current = () => {
      try {
        return (
          this.alive &&
          this.host.selectedRecord?.["id"] === id &&
          this.host.api.environment === scope &&
          JSON.stringify(this.host.identity) === identity
        );
      } catch {
        return false;
      }
    };
    this.busy = true;
    this.error = "";
    try {
      await this.host.api.request(
        scope + "/workers/" + id + "/" + action,
        "POST",
        { expected_revision: revision },
        { "Idempotency-Key": crypto.randomUUID() },
      );
      // Replayed commands return their original snapshot; read current lease counts.
      const result = await this.host.api.request<Record<string, unknown>>(
        scope + "/workers/" + id,
      );
      if (current()) this.host.selectedRecord = result;
    } catch (error) {
      if (current()) this.error = describeError(error).message;
    } finally {
      if (this.alive) {
        this.busy = false;
        this.cdr.markForCheck();
      }
    }
  }
  ngOnChanges(changes: SimpleChanges) {
    if (
      changes["record"]?.previousValue?.["id"] !==
      changes["record"]?.currentValue?.["id"]
    )
      this.error = "";
  }
  ngOnDestroy() {
    this.alive = false;
    clearInterval(this.timer);
  }
}
