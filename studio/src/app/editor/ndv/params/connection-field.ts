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
// Select a workflow slot or open the existing connection setup entry point.
import { ChangeDetectionStrategy, Component, input } from "@angular/core";
import { Select, type SelectOption } from "../../../forms/ui/select";
import type { ParamSpec } from "../registry";
import type { FormSession } from "./form-session";
import { NO_MANAGE, slotBase, slotsOf } from "./slots";
const NEW_SLOT = "\u0000new";
const CREATE = "\u0000create";
@Component({
  selector: "weave-connection-field",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Select],
  template: `<weave-select
    [label]="spec().label"
    [hideLabel]="true"
    [controlId]="controlId()"
    [options]="options()"
    [value]="value()"
    [disabled]="!!readOnly()"
    [describedBy]="describedBy()"
    placeholder="Choose a slot"
    (choose)="choose($event)"
  />`,
})
export class ConnectionField {
  session = input.required<FormSession>();
  spec = input.required<ParamSpec>();
  controlId = input.required<string>();
  describedBy = input("");
  readOnly = input<string | null>(null);
  value(): string {
    const value = this.session().read(this.spec());
    return value.mode === "fixed" && typeof value.value === "string"
      ? value.value
      : "";
  }
  options(): SelectOption[] {
    const host = this.session().host,
      connector = this.session().connector(this.spec());
    const all = slotsOf(host.model.definition),
      slots = all.filter((slot) => !connector || slot.connector === connector);
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
    if (option && !option.disabled)
      session.write(spec, { mode: "fixed", value });
  }
}
