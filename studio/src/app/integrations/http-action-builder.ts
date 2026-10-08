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
// API action builder: a dialog that turns one described HTTPS request, or
// operations imported from an OpenAPI document, into Actions on the built-in
// weave-http@2.0.0 connector. Load it lazily (@defer); it only emits what the
// person chose, and the host editor inserts steps and connection slots.
import {
  Component,
  computed,
  inject,
  input,
  output,
  signal,
  viewChild,
} from "@angular/core";
import type { StudioApi } from "../api";
import { DialogService, Modal } from "../dialog";
import { Icon } from "../icon";
import {
  HttpActionDescribe,
  type DescribePrefill,
} from "./http-action-describe";
import { HttpActionOpenApi } from "./http-action-openapi";
import {
  HttpActionReview,
  type HttpActionUse,
  type PublishedAction,
  type UseContext,
} from "./http-action-review";

export type { DescribePrefill } from "./http-action-describe";
export type {
  HttpActionUse,
  PublishedAction,
  UseContext,
} from "./http-action-review";
export type BuilderTab = "describe" | "openapi";

let sequence = 0;

@Component({
  selector: "weave-http-action-builder",
  standalone: true,
  imports: [
    Modal,
    Icon,
    HttpActionDescribe,
    HttpActionOpenApi,
    HttpActionReview,
  ],
  template: `<weave-modal
    heading="New API action"
    [wide]="true"
    closeLabel="Close the API action builder"
    [describedBy]="prefix + '-intro'"
    (dismiss)="close()"
  >
    <div class="hb-shell">
      <p class="hint hb-intro" [id]="prefix + '-intro'">
        Describe one HTTPS request, or import operations from an OpenAPI
        document. Studio turns them into actions on the built-in HTTP connector,
        with no code.
      </p>
      <ng-content select="[readiness]" />
      <div
        class="hb-tabs"
        role="tablist"
        aria-label="How to create the action"
        (keydown)="tabKey($event)"
      >
        @for (t of tabs; track t.id) {
          <button
            type="button"
            role="tab"
            [id]="prefix + '-tab-' + t.id"
            [attr.aria-selected]="tab() === t.id"
            [attr.aria-controls]="prefix + '-panel-' + t.id"
            [attr.tabindex]="tab() === t.id ? 0 : -1"
            [class.selected]="tab() === t.id"
            [attr.data-initial-focus]="
              startTab() === 'openapi' && t.id === 'openapi' ? '' : null
            "
            (click)="select(t.id)"
          >
            {{ t.label }}
          </button>
        }
      </div>
      <div
        role="tabpanel"
        class="hb-panel"
        [id]="prefix + '-panel-describe'"
        [attr.aria-labelledby]="prefix + '-tab-describe'"
        [hidden]="tab() !== 'describe'"
      >
        <weave-http-action-describe
          [api]="api()"
          [prefill]="prefill()"
          [initialFocus]="startTab() === 'describe'"
        />
      </div>
      <div
        role="tabpanel"
        class="hb-panel"
        [id]="prefix + '-panel-openapi'"
        [attr.aria-labelledby]="prefix + '-tab-openapi'"
        [hidden]="tab() !== 'openapi'"
      >
        <weave-http-action-openapi [api]="api()" />
      </div>
      <weave-http-action-review
        [api]="api()"
        [candidate]="candidate()"
        [connected]="connected()"
        [canCompile]="canCompile()"
        [canPublish]="canPublish()"
        [context]="context()"
        [readonly]="readonly()"
        [publishedVersions]="publishedVersions()"
        (use)="route($event)"
        (publishedAction)="publishedOne($event)"
        (bump)="bump($event)"
      />
      <!-- Always in view: Cancel, the platform check and the one next step. -->
      @let review = reviewPanel();
      <div class="dialog-footer hb-footer">
        <button type="button" class="tertiary" (click)="close()">
          {{ review?.published() ? "Close" : "Cancel" }}
        </button>
        @if (connected() && review?.yaml()) {
          <button
            type="button"
            class="hb-check"
            [attr.aria-disabled]="!canCompile() || review?.busy() || null"
            [attr.aria-describedby]="
              !canCompile() ? review?.prefix + '-nocompile' : null
            "
            (click)="canCompile() && review?.runCheck()"
          >
            <weave-icon name="check" />Check with the platform
          </button>
        }
        @if (review?.primary()) {
          <button
            type="button"
            class="primary"
            [attr.aria-disabled]="
              (review?.primary() === 'publish' && review?.busy()) || null
            "
            (click)="review?.runPrimary()"
          >
            {{ review?.primaryLabel() }}
          </button>
        } @else if (review?.publishPending()) {
          <!-- The next step shows before it can run, and says why it can't. -->
          <span class="hb-blocked" role="status" [id]="prefix + '-blocked'">{{
            blocked() ? review?.pendingReason() : ""
          }}</span>
          <button
            type="button"
            class="primary"
            aria-disabled="true"
            [attr.aria-describedby]="blocked() ? prefix + '-blocked' : null"
            (click)="blocked.set(true)"
          >
            Publish action
          </button>
        }
      </div>
    </div>
  </weave-modal>`,
  styles: [
    `
      .hb-intro {
        margin: 0 0 12px;
        line-height: 1.5;
      }
      .hb-tabs {
        display: flex;
        gap: 20px;
        border-bottom: 1px solid var(--line);
        margin: 4px 0 16px;
        overflow-x: auto;
      }
      .hb-tabs button {
        border: 0;
        background: transparent;
        border-radius: 0;
        position: relative;
        padding: 0 4px;
        color: var(--muted);
        font-weight: 500;
        min-height: 40px;
      }
      .hb-tabs button.selected {
        color: var(--text);
        font-weight: 600;
      }
      .hb-tabs button.selected::after {
        content: "";
        position: absolute;
        left: 0;
        right: 0;
        bottom: 0;
        height: 3px;
        background: var(--accent);
      }
      .hb-panel[hidden] {
        display: none;
      }
      .hb-footer .primary {
        margin-left: 4px;
      }
      .hb-blocked {
        flex: 1 1 auto;
        min-width: 0;
        font: var(--type-small);
        color: var(--muted);
        text-align: right;
      }
      .hb-blocked:empty {
        display: none;
      }
    `,
  ],
})
export class HttpActionBuilder {
  /** The paired Studio API (local host and platform bridge). */
  api = input.required<StudioApi>();
  /** A platform is connected with a workspace selected. */
  connected = input(false);
  /** The account holds `compile` in this workspace. */
  canCompile = input(false);
  /** The account holds `definition.publish` in this workspace. */
  canPublish = input(false);
  /** Where the builder was opened: a selected action step, the workflow, or neither. */
  context = input<UseContext>("workflow");
  /** The workflow can't be edited: no insert buttons. */
  readonly = input(false);
  /** Published name@version references, to suggest a free version after a conflict. */
  publishedVersions = input<readonly string[]>([]);
  prefill = input<DescribePrefill | null>(null);
  startTab = input<BuilderTab>("describe");

  /** "Use in this step" (context "step"). */
  useInStep = output<HttpActionUse>();
  /** "Insert into workflow" (context "workflow"). */
  insertIntoWorkflow = output<HttpActionUse>();
  /** An action was published: refresh the catalog and drop cached contracts. */
  published = output<PublishedAction>();
  closed = output<void>();

  readonly tabs: { id: "describe" | "openapi"; label: string }[] = [
    { id: "describe", label: "Describe a request" },
    { id: "openapi", label: "Import OpenAPI" },
  ];
  prefix = `http-builder-${++sequence}`;
  private chosenTab = signal<"describe" | "openapi" | null>(null);
  tab = computed(() => this.chosenTab() ?? this.startTab());
  private describe = viewChild(HttpActionDescribe);
  private openapi = viewChild(HttpActionOpenApi);
  private review = viewChild(HttpActionReview);
  /** The review panel, for the footer's commands. */
  reviewPanel() {
    return this.review() ?? null;
  }
  /** "Publish action" was pressed before there was an action: say why. */
  blocked = signal(false);
  private dialogs = inject(DialogService);

  candidate = computed(() =>
    this.tab() === "describe"
      ? (this.describe()?.candidate() ?? null)
      : (this.openapi()?.candidate() ?? null),
  );

  select(tab: "describe" | "openapi") {
    this.chosenTab.set(tab);
  }
  tabKey(event: KeyboardEvent) {
    const order = this.tabs.map((t) => t.id);
    const index = order.indexOf(this.tab());
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % order.length;
    else if (event.key === "ArrowLeft")
      next = (index - 1 + order.length) % order.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = order.length - 1;
    else return;
    event.preventDefault();
    this.select(order[next]);
    document.getElementById(`${this.prefix}-tab-${order[next]}`)?.focus();
  }
  route(use: HttpActionUse) {
    if (this.context() === "step") this.useInStep.emit(use);
    else this.insertIntoWorkflow.emit(use);
  }
  publishedOne(action: PublishedAction) {
    this.published.emit(action);
  }
  bump(version: string) {
    if (this.tab() === "describe") this.describe()?.setVersion(version, true);
    else {
      this.openapi()?.setVersion(version);
      this.review()?.restoreFocus();
    }
  }
  /**
   * Work that closing would lose: anything entered on the describe tab that
   * isn't published as it stands, and a pasted document whose created
   * actions aren't all published.
   */
  unsaved() {
    const review = this.review();
    const describe = this.describe();
    const openapi = this.openapi();
    const described =
      !!describe?.hasInput() &&
      !review?.isPublished(describe.candidate().document);
    const documents = openapi?.documents() ?? [];
    const imported =
      !!openapi?.source().trim() &&
      !(documents.length && documents.every((d) => review?.isPublished(d)));
    return described || imported;
  }
  /** Asks before discarding unsaved work; nothing is kept after closing. */
  async close() {
    if (this.unsaved()) {
      const opener = document.activeElement;
      const close = await this.dialogs.confirm({
        title: "Close the API action builder?",
        message:
          "Nothing you entered here is saved. Download or publish the action first if you want to keep it.",
        confirmLabel: "Close builder",
        cancelLabel: "Keep editing",
        danger: true,
      });
      if (!close) {
        // The shared dialog doesn't return focus inside another dialog.
        setTimeout(() => {
          const active = document.activeElement;
          if (active && active !== document.body) return;
          if (opener instanceof HTMLElement && opener.isConnected)
            opener.focus();
          else
            document
              .getElementById(`${this.prefix}-tab-${this.tab()}`)
              ?.focus();
        });
        return;
      }
    }
    this.closed.emit();
  }
}
