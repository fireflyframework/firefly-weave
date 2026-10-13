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
// About this step: its ID (with Copy), where it sits, and for an action what
// runs it and what it does to data. Read-only.
import { ChangeDetectionStrategy, Component, input } from "@angular/core";
import { Icon } from "../../../icon";
import type { FormSession } from "../params/form-session";
import { aboutRows, placementText } from "./settings-rules";

let sequence = 0;

@Component({
  selector: "weave-about-section",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon],
  host: { "[attr.data-param]": "'about'" },
  template: `@let session = this.session();
    <section class="param settings-about" [attr.aria-labelledby]="id">
      <div class="param-label-row">
        <h4 class="param-label" [id]="id">About this step</h4>
      </div>
      <dl class="param-control settings-about-list">
        @if (!session.target.startsWith("$")) {
          <dt>Step ID</dt>
          <dd>
            <code>{{ session.target }}</code>
            <button
              type="button"
              class="icon-button settings-copy"
              aria-label="Copy step ID"
              title="Copy step ID"
              (click)="copy()"
            >
              <weave-icon name="copy" [size]="16" />
            </button>
          </dd>
        }
        @if (placement(); as where) {
          <dt>Placement</dt>
          <dd>{{ where }}</dd>
        }
        @for (row of rows(); track row[0]) {
          <dt>{{ row[0] }}</dt>
          <dd>{{ row[1] }}</dd>
        }
      </dl>
    </section>`,
})
export class AboutSection {
  session = input.required<FormSession>();
  readonly id = `sd-about-${++sequence}`;

  placement(): string {
    const session = this.session();
    return placementText(session.host.model.definition, session.target);
  }
  rows(): [string, string][] {
    const session = this.session();
    const step = session.controller.step(session.target);
    return step ? aboutRows(step, session.controller.kindContext()) : [];
  }
  async copy() {
    const session = this.session();
    const id = session.target;
    try {
      await navigator.clipboard.writeText(id);
      if (session.isCurrent()) session.announce(`Copied ${id}.`);
    } catch {
      if (session.isCurrent())
        session.announce("Studio couldn't reach the clipboard.");
    }
  }
}
