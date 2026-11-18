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
// Working locally: select a workflow slot or open connection setup.
// Connected to an environment: choose one of the connections this person may
// bind there; the slot behind it follows, the choice is kept on this computer
// and tested, and the pill says how the last test went.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  viewChild,
  type DoCheck,
} from "@angular/core";
import { Icon } from "../../../icon";
import { Select, type SelectOption } from "../../../forms/ui/select";
import type { ParamSpec } from "../registry";
import type { FormSession } from "./form-session";
import { NO_MANAGE, slotBase, slotsOf } from "./slots";
import {
  CREATE_CONNECTION,
  connectionOptions,
  notEncrypted,
  testPill,
  type TestPill,
} from "../../connections/connection-choices";
import type { ConnectionView } from "../../connections/connection-view";
const NEW_SLOT = "\u0000new";
const CREATE = "\u0000create";
@Component({
  selector: "weave-connection-field",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Select, Icon],
  template: `@if (connected(); as view) {
      <div class="connection-row">
        <weave-select
          class="connection-choice"
          icon="chevronDown"
          [label]="spec().label"
          [hideLabel]="true"
          [controlId]="controlId()"
          [options]="connectedOptions(view)"
          [value]="view.binding?.connectionId ?? ''"
          [disabled]="!!readOnly()"
          [describedBy]="describedBy()"
          [placeholder]="
            view.list ? 'Choose a connection' : 'Loading connections…'
          "
          (choose)="chooseConnected($event, view)"
        />
        <span class="connection-status" role="status">
          @if (view.testing) {
            <span class="connection-pill"
              ><weave-icon name="spinner" [size]="16" />Testing…</span
            >
          } @else if (pill(view); as shown) {
            <span
              class="connection-pill"
              [class.is-success]="shown.tone === 'success'"
              [class.is-danger]="shown.tone === 'danger'"
              ><weave-icon
                [name]="shown.tone === 'success' ? 'ok' : 'failCircle'"
                [size]="16"
              />{{ shown.text }}</span
            >
          }
        </span>
        @if (retryable(view)) {
          <button
            type="button"
            class="tertiary connection-retry"
            (click)="retry()"
          >
            Retry
          </button>
        }
        @if (view.item && plain(view.item.origin)) {
          <span class="connection-plain">Not encrypted</span>
        }
      </div>
    } @else {
      <weave-select
        icon="chevronDown"
        [label]="spec().label"
        [hideLabel]="true"
        [controlId]="controlId()"
        [options]="options()"
        [value]="value()"
        [disabled]="!!readOnly()"
        [describedBy]="describedBy()"
        placeholder="Choose a slot"
        (choose)="choose($event)"
      />
    }`,
  styles: `
    .connection-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--space-2);
      min-width: 0;
    }
    .connection-choice {
      flex: 1 1 240px;
      min-width: 0;
    }
    .connection-status:empty {
      display: none;
    }
    .connection-pill,
    .connection-plain {
      display: inline-flex;
      align-items: center;
      gap: var(--space-1);
      min-height: 24px;
      padding: 0 var(--space-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-pill);
      color: var(--text-muted);
      font: var(--type-caption);
      white-space: nowrap;
    }
    .connection-pill.is-success {
      border-color: var(--success-bd);
      background: var(--success-bg);
      color: var(--success-ink);
    }
    .connection-pill.is-danger {
      border-color: var(--danger-bd);
      background: var(--danger-bg);
      color: var(--danger-ink);
    }
    .connection-plain {
      border-color: var(--warning-bd);
      background: var(--warning-bg);
      color: var(--warning-ink);
    }
    .connection-retry {
      min-height: var(--sd-target, var(--control-sm));
    }
    @media (max-width: 767px), (pointer: coarse) {
      .connection-choice {
        flex-basis: 100%;
      }
    }
  `,
})
export class ConnectionField implements DoCheck {
  session = input.required<FormSession>();
  spec = input.required<ParamSpec>();
  controlId = input.required<string>();
  describedBy = input("");
  readOnly = input<string | null>(null);
  private readonly choices = viewChild(Select);
  /** What was last settled (preselection, a gone connection, a due test), and the last open request seen. */
  private settledFor = "";
  private openSeen = -1;

  /** Connected to an environment: what the field shows; null working locally. */
  connected(): ConnectionView | null {
    return this.session().connectionView(this.spec());
  }
  connectedOptions(view: ConnectionView): SelectOption[] {
    if (!view.connector)
      return [
        {
          value: "\u0000action",
          label:
            "Choose the action first. The connection follows its connector.",
          disabled: true,
        },
      ];
    if (!view.list) return [];
    return connectionOptions(view.list.items ?? [], view.environment.name, {
      manage: view.canManage,
      connector: view.connector,
    });
  }
  pill(view: ConnectionView): TestPill | null {
    return view.canManage ? testPill(view.binding?.tested, new Date()) : null;
  }
  retryable(view: ConnectionView): boolean {
    return (
      !this.readOnly() &&
      !view.testing &&
      view.canManage &&
      !!this.pill(view)?.retry
    );
  }
  plain(origin: string | null): boolean {
    return notEncrypted(origin);
  }
  chooseConnected(value: string, view: ConnectionView): void {
    const session = this.session(),
      spec = this.spec();
    if (this.readOnly() || !session.isCurrent()) return;
    if (value === CREATE_CONNECTION)
      return session.openConnectionSheet(spec, "create");
    const item = view.list?.items?.find(
      (entry) => entry.connectionId === value,
    );
    if (item?.enabled) void session.chooseConnection(spec, item);
  }
  retry(): void {
    if (!this.readOnly()) void this.session().retestConnection(this.spec());
  }
  ngDoCheck(): void {
    const view = this.connected();
    if (!view) return;
    const key = [
      view.port.key,
      view.slot,
      view.binding?.connectionId ?? "",
      view.list ? "listed" : "",
    ].join("\u0000");
    if (key !== this.settledFor && !this.readOnly()) {
      this.settledFor = key;
      const session = this.session(),
        spec = this.spec();
      queueMicrotask(() => void session.settleConnection(spec));
    }
    const request = view.port.opening();
    if (request && request.seq !== this.openSeen) {
      this.openSeen = request.seq;
      if (request.slot === view.slot)
        queueMicrotask(() => {
          document.getElementById(this.controlId())?.focus();
          this.choices()?.show();
        });
    }
  }
  value(): string {
    const value = this.session().read(this.spec());
    return value.mode === "fixed" && typeof value.value === "string"
      ? value.value
      : "";
  }
  options(): SelectOption[] {
    const host = this.session().host,
      connector = this.session().connector(this.spec());
    // A step that calls its own file action moves between file servers.
    const servers = this.session().fileServers(this.spec());
    const all = slotsOf(host.model.definition),
      slots = all.filter(
        (slot) =>
          !connector ||
          slot.connector === connector ||
          !!servers?.has(slot.connector),
      );
    const options: SelectOption[] = slots.map((slot) => ({
      value: slot.name,
      label: slot.name,
      description: slot.connector,
    }));
    const current = this.value();
    if (current && !slots.some((slot) => slot.name === current)) {
      const imported = all.find((slot) => slot.name === current);
      options.unshift({
        value: current,
        label: current,
        description: imported
          ? `This slot uses ${imported.connector || "an unspecified connector"}, not ${connector}.`
          : "Not declared in this workflow",
      });
    }
    options.push({
      value: NEW_SLOT,
      label: "New slot",
      description: connector
        ? `Declares a ${slotBase(connector)} slot`
        : "Choose the action first",
    });
    if (host.profile)
      options.push({
        value: CREATE,
        label: "Create connection",
        description: host.can("connection.manage")
          ? "Opens connection setup. Bind it when you activate."
          : NO_MANAGE,
        disabled: !host.can("connection.manage"),
      });
    return options;
  }
  choose(value: string): void {
    const session = this.session(),
      spec = this.spec();
    if (this.readOnly() || !session.isCurrent()) return;
    if (value === NEW_SLOT)
      return session.newSlot(spec, session.connector(spec));
    if (value === CREATE) return session.createConnection(spec);
    const option = this.options().find((option) => option.value === value);
    if (!option || option.disabled) return;
    const slot = slotsOf(session.host.model.definition).find(
      (item) => item.name === value,
    );
    if (slot && slot.connector !== session.connector(spec))
      if (session.fileServers(spec)?.has(slot.connector))
        return void session.useFileServer(spec, value);
    session.write(spec, { mode: "fixed", value });
  }
}
