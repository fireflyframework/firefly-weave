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
  output,
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
    aria-label="Action shortcuts"
  >
    <section class="palette-reuse" [attr.aria-labelledby]="prefix + '-title'">
      <div class="palette-section-heading">
        <h4 class="palette-subheading" [id]="prefix + '-title'">
          Use an existing action
        </h4>
        <details
          class="palette-action-help"
          (keydown.escape)="closeHelp($event)"
        >
          <summary
            aria-label="About existing actions"
            title="About existing actions"
          >
            i
          </summary>
          <p>
            Published actions are reusable steps shared in your project. Choose
            one to add it to this workflow with its saved settings.
          </p>
        </details>
      </div>
      @switch (state()) {
        @case ("offline") {
          <p class="pane-help">Connect to browse your project's actions.</p>
          <button
            type="button"
            class="palette-connect"
            (click)="connectPlatform()"
          >
            Connect to a platform
          </button>
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
    </section>
    <section class="palette-create" [attr.aria-labelledby]="prefix + '-create'">
      <div class="palette-section-heading">
        <h4 class="palette-subheading" [id]="prefix + '-create'">
          Create an API action
        </h4>
        <details
          class="palette-action-help"
          (keydown.escape)="closeHelp($event)"
        >
          <summary
            aria-label="About creating an API action"
            title="About creating an API action"
          >
            i
          </summary>
          <p>
            Describe an API request or import its OpenAPI definition. You can
            configure and save an action file on this computer. Connect to
            publish it for your project.
          </p>
        </details>
      </div>
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
    </section>
  </section>`,
  styles: [
    `
      .palette-integrations {
        margin-top: 6px;
        padding: 6px 0 0 8px;
        border-left: 2px solid var(--line);
      }
      .palette-create {
        margin-top: 14px;
        padding-top: 12px;
        border-top: 1px solid var(--line);
      }
      .palette-section-heading {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px;
        margin-bottom: 8px;
      }
      .palette-section-heading h4 {
        flex: 1;
        min-width: 0;
      }
      .palette-action-help {
        min-width: 0;
      }
      .palette-action-help > summary {
        display: grid;
        place-items: center;
        width: 24px;
        min-height: 24px;
        border: 1px solid var(--border);
        border-radius: 50%;
        color: var(--muted);
        font: 600 12px/1 var(--font-sans);
        cursor: pointer;
        list-style: none;
      }
      .palette-action-help > summary::-webkit-details-marker {
        display: none;
      }
      .palette-action-help[open] {
        flex-basis: 100%;
      }
      .palette-action-help p {
        margin: 8px 0 0;
        font: var(--type-caption);
        color: var(--muted);
        overflow-wrap: anywhere;
      }
      .palette-connect {
        width: 100%;
        height: auto;
        min-height: 36px;
        padding: 6px 8px;
        white-space: normal;
        font-size: 12px;
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
  connect = output<void>();

  async connectPlatform() {
    if (await this.host().ensureApplied()) this.connect.emit();
  }
  closeHelp(event: Event) {
    const details = event.currentTarget as HTMLDetailsElement;
    details.open = false;
    details.querySelector("summary")?.focus();
    event.stopPropagation();
  }

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
