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
// The palette's "Published actions" (WP-19): published actions that match
// the palette search, each inserted with one click together with its
// connection slot (one undo step), and the entry to the API action builder.
// Load it lazily (@defer); the shell passes itself as the EditorHost.
import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  input,
} from "@angular/core";
import { Icon } from "../icon";
import { searchActions, type ActionPickerItem } from "./action-picker";
import { catalogItems } from "./catalog-picker";
import { insertCatalogAction } from "./editor-bridge";
import type { EditorHost } from "./editor-host";

/** Actions listed at once; the palette search narrows the rest. */
const SHOWN = 6;
const none = { groups: [], count: 0, hidden: 0 };

let sequence = 0;

@Component({
  selector: "weave-palette-integrations",
  // A view of the shell's state: checked whenever the shell renders.
  changeDetection: ChangeDetectionStrategy.Eager,
  standalone: true,
  imports: [Icon],
  template: `<section
    class="palette-integrations"
    [attr.aria-labelledby]="prefix + '-title'"
  >
    <h4 class="palette-subheading" [id]="prefix + '-title'">
      Published actions
    </h4>
    @switch (state()) {
      @case ("offline") {
        <p class="pane-help">Connect to a platform to use published actions.</p>
      }
      @case ("loading") {
        <p class="pane-help" role="status">Loading published actions…</p>
      }
      @case ("forbidden") {
        <p class="pane-help">
          Your account can't read the published actions in this workspace.
        </p>
      }
      @case ("error") {
        <div class="pane-help" role="alert">
          <p>
            Published actions couldn't be loaded.
            {{ host().catalogError?.message }}
          </p>
          <button
            type="button"
            class="palette-retry"
            (click)="host().loadActionCatalog(false, true)"
          >
            Try again
          </button>
        </div>
      }
      @default {
        @if (!results().count) {
          <p class="pane-help">
            {{
              query().trim()
                ? "No published action matches the search."
                : "This project has no published actions yet."
            }}
          </p>
        }
      }
    }
    @for (group of results().groups; track group.key) {
      @for (item of group.items; track item.name + "@" + item.version) {
        <button
          type="button"
          class="palette-step palette-action"
          [attr.data-uses]="item.name + '@' + item.version"
          [disabled]="host().model.readonly"
          [attr.aria-label]="
            'Insert ' +
            item.name +
            '@' +
            item.version +
            (item.operation ? ', ' + item.operation : '')
          "
          (click)="pick(item)"
        >
          <span class="step-icon"><weave-icon name="action" /></span
          ><span class="palette-label"
            >{{ item.name
            }}<small
              >{{ item.version
              }}{{ item.operation ? " · " + item.operation : ""
              }}{{ group.key === "other" ? "" : " · " + group.label }}</small
            ></span
          >
        </button>
      }
    }
    @if (results().hidden) {
      <p class="pane-help">
        {{ results().hidden }} more. Search the steps to narrow the list.
      </p>
    }
    @if (canCreate()) {
      <button
        type="button"
        class="palette-new-api"
        [disabled]="host().model.readonly"
        (click)="host().openApiBuilder('workflow')"
      >
        <weave-icon name="plus" />New API action
      </button>
    } @else {
      <p class="pane-help">
        To call another API, ask a developer to publish an action.
      </p>
    }
  </section>`,
  styles: [
    `
      .palette-integrations {
        margin-top: 6px;
        padding: 6px 0 0 8px;
        border-left: 2px solid var(--line);
      }
      .palette-subheading {
        margin: 0;
        font: 600 12px/16px var(--font-sans);
        color: var(--text);
      }
      .pane-help {
        margin: 8px 0 12px;
      }
      .pane-help p {
        margin: 0 0 6px;
      }
      .palette-action {
        min-height: 40px;
        cursor: pointer;
      }
      .palette-action small {
        display: block;
        font: var(--type-caption);
        font-weight: 400;
        color: var(--muted);
      }
      .palette-action .palette-label {
        overflow-wrap: anywhere;
      }
      .palette-retry {
        min-height: 32px;
        font-size: 12px;
      }
      .palette-new-api {
        width: 100%;
        justify-content: flex-start;
        gap: 8px;
        font-size: 12px;
      }
      .palette-new-api weave-icon {
        width: 16px;
        height: 16px;
      }
    `,
  ],
})
export class PaletteIntegrations implements OnInit {
  /** The editor shell. */
  host = input.required<EditorHost>();
  /** The palette search text. */
  query = input("");

  prefix = `palette-integrations-${++sequence}`;
  private memo: {
    rows: unknown;
    contracts: unknown;
    query: string;
    value: ReturnType<typeof searchActions>;
  } | null = null;

  ngOnInit() {
    // The catalog loads once per workspace; the shell ignores repeats.
    void this.host().loadActionCatalog();
  }
  state() {
    const host = this.host();
    if (!host.profile) return "offline";
    if (!host.identity) return "loading";
    if (!host.can("catalog.read")) return "forbidden";
    if (host.catalogState === "error") return "error";
    if (host.catalogState !== "ready") return "loading";
    return "ready";
  }
  /** "New API action": offline it makes a placeholder and a file; connected it needs publishing. */
  canCreate() {
    const host = this.host();
    return !host.profile || host.can("definition.publish");
  }
  results() {
    if (this.state() !== "ready") return none;
    const host = this.host();
    const memo = this.memo;
    if (
      memo &&
      memo.rows === host.actionVersions &&
      memo.contracts === host.catalogContracts &&
      memo.query === this.query()
    )
      return memo.value;
    const value = searchActions(
      this.query(),
      catalogItems(host.actionVersions, host.catalogContracts),
      SHOWN,
    );
    this.memo = {
      rows: host.actionVersions,
      contracts: host.catalogContracts,
      query: this.query(),
      value,
    };
    return value;
  }
  pick(item: ActionPickerItem) {
    void insertCatalogAction(this.host(), {
      uses: `${item.name}@${item.version}`,
      id: item.id,
    });
  }
}
