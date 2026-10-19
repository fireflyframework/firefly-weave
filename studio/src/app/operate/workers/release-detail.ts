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
// One release: its image and task types (releases.read), and what runs it
// (releases.usage): its workers by state, the activations that pin it and the
// deployment components on targets this person may read. The platform leaves
// out every other target; Studio shows only what it answers. Reads again
// every 10 s while open.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  OnChanges,
  OnDestroy,
  OnInit,
  SimpleChanges,
  inject,
  input,
  output,
} from "@angular/core";
import type { App } from "../../app";
import { describeError, type PlainError } from "../../errors";
import { shortId } from "../../format";
import { Icon } from "../../icon";
import { MAX_PAGES } from "../operate-limits";
import { environmentOf, scopeKeyOf } from "../operate-list";
import { OperateState } from "../operate-state";
import { Poller } from "../operate-store";
import {
  releaseTaskTypes,
  releaseUsageQuery,
  shortDigest,
  type ReleaseUsage,
  type WorkerRelease,
} from "./workers-model";

type Deployment = ReleaseUsage["deployments"][number];

@Component({
  selector: "weave-release-detail",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, OperateState],
  styleUrls: ["../operate.css", "./workers.css"],
  template: `<header class="detail-header">
      <h2 id="release-detail-title" tabindex="-1">
        Release
        {{ release ? digest(release.image_digest) : short(releaseId()) }}
      </h2>
      <button
        type="button"
        class="icon-button"
        aria-label="Close detail"
        (click)="closed.emit()"
      >
        <weave-icon name="close" />
      </button>
    </header>
    @if (problem; as failed) {
      @if (failed.status === 403) {
        <weave-operate-state
          kind="access"
          heading="You don't have access to this release"
          capability="catalog.read"
        />
      } @else if (failed.status === 404) {
        <weave-operate-state
          kind="empty"
          heading="This release can't be found"
          text="It may belong to another environment. Choose a release from the list."
        />
      } @else {
        <weave-operate-state
          kind="error"
          [message]="failed.message"
          [code]="failed.code"
          (retry)="read()"
        />
      }
    } @else if (!release) {
      <weave-operate-state kind="loading" noun="the release" [rows]="3" />
    } @else {
      <dl class="facts">
        <dt>Image digest</dt>
        <dd>
          <code class="mono-id">{{ release.image_digest }}</code>
        </dd>
        <dt>Task types</dt>
        <dd>{{ types() }}</dd>
        @if (release.credential_capabilities?.length) {
          <dt>Uses connections for</dt>
          <dd>{{ release.credential_capabilities!.join(", ") }}</dd>
        }
      </dl>
      <h3 id="release-workers-title">Workers</h3>
      @if (usageProblem; as failed) {
        <weave-operate-state
          [kind]="failed.status === 403 ? 'access' : 'partial'"
          heading="You don't have access to what runs this release"
          capability="catalog.read"
          [message]="failed.message"
          [code]="failed.code"
          (retry)="read()"
        />
      } @else if (!usage) {
        <weave-operate-state kind="loading" noun="its workers" [rows]="2" />
      } @else {
        <ul class="release-workers" aria-labelledby="release-workers-title">
          @for (state of states; track state.key) {
            <li>
              <span class="figure numeric">{{ usage.workers[state.key] }}</span
              ><span class="figure-label">{{ state.label }}</span
              ><span
                class="meter-track"
                [attr.data-state]="state.key"
                aria-hidden="true"
                ><span
                  class="meter-fill"
                  [style.width.%]="share(usage.workers[state.key])"
                ></span
              ></span>
            </li>
          }
        </ul>
        <div class="action-row">
          <button
            type="button"
            class="tertiary"
            (click)="showWorkers.emit(release.id)"
          >
            Show its workers
          </button>
        </div>
        <dl class="facts">
          <dt>Activations that pin it</dt>
          <dd class="numeric">{{ usage.activations }}</dd>
        </dl>
        <h3 id="release-used-by-title">Used by</h3>
        @if (deployments.length) {
          <div
            class="resource-table usage-table"
            role="table"
            aria-labelledby="release-used-by-title"
          >
            <div role="rowgroup">
              <div class="table-head" role="row">
                <span role="columnheader">Target › deployment › component</span
                ><span role="columnheader" class="numeric-cell">Replicas</span>
              </div>
            </div>
            <div role="rowgroup">
              @for (row of deployments; track rowKey(row)) {
                <div class="resource-row" role="row">
                  <span
                    role="cell"
                    class="cell-text"
                    [attr.title]="path(row)"
                    >{{ path(row) }}</span
                  ><span role="cell" class="numeric numeric-cell">{{
                    row.replicas
                  }}</span>
                </div>
              }
            </div>
          </div>
          @if (usageCursor && pages < maxPages) {
            <button
              type="button"
              class="load-more"
              [disabled]="appending"
              (click)="moreUsage()"
            >
              Load more deployments
            </button>
          }
        } @else if (usage.deployments_hidden) {
          <p class="hint">
            You need <code>deployment.read</code> on a target to see where this
            release runs.
          </p>
        } @else {
          <p class="hint">
            No deployment on a target you can see runs this release.
          </p>
        }
      }
      <div class="action-row">
        <button
          type="button"
          aria-disabled="true"
          aria-describedby="release-rollout-hint"
        >
          Roll out to a target…
        </button>
      </div>
      <p class="hint" id="release-rollout-hint">
        Rolling a release out from Studio comes with change requests. Until
        then, update the deployment in Clusters or with the CLI.
      </p>
      <details class="disclosure">
        <summary>Technical details</summary>
        <dl class="facts">
          <dt>Release ID</dt>
          <dd>
            <code class="mono-id">{{ release.id }}</code>
          </dd>
        </dl>
      </details>
    }`,
})
export class ReleaseDetail implements OnChanges, OnInit, OnDestroy {
  host = input.required<App>();
  releaseId = input.required<string>();
  /** The fleet, narrowed to this release's workers. */
  showWorkers = output<string>();
  closed = output<void>();
  private cdr = inject(ChangeDetectorRef);
  readonly short = shortId;
  readonly digest = shortDigest;
  readonly maxPages = MAX_PAGES;
  readonly states = [
    { key: "online", label: "Online" },
    { key: "offline", label: "Offline" },
    { key: "draining", label: "Draining" },
    { key: "revoked", label: "Revoked" },
  ] as const;
  readonly poller = new Poller(() => this.read(), 10_000);
  release: WorkerRelease | null = null;
  usage: ReleaseUsage | null = null;
  deployments: Deployment[] = [];
  usageCursor: string | null = null;
  pages = 1;
  appending = false;
  problem: PlainError | null = null;
  usageProblem: PlainError | null = null;
  /** Bumped for another release or scope: older answers change nothing. */
  private generation = 0;

  ngOnInit() {
    this.poller.start(false);
  }
  ngOnDestroy() {
    this.poller.stop();
    this.generation++;
  }
  ngOnChanges(changes: SimpleChanges) {
    if (!changes["releaseId"]) return;
    this.generation++;
    this.release = null;
    this.usage = null;
    this.deployments = [];
    this.usageCursor = null;
    this.pages = 1;
    this.problem = null;
    this.usageProblem = null;
    void this.poller.refresh();
  }
  /** The release, then its usage: as many deployment pages as are on screen. */
  async read(): Promise<boolean> {
    const host = this.host();
    const scope = environmentOf(host);
    const id = this.releaseId();
    if (!scope || !id) return false;
    const generation = this.generation;
    const key = scopeKeyOf(host);
    const current = () =>
      generation === this.generation && key === scopeKeyOf(host);
    const base = `${scope}/worker-releases/${encodeURIComponent(id)}`;
    try {
      const release = await host.api.request<WorkerRelease>(base);
      if (!current()) return false;
      this.release = release;
      this.problem = null;
    } catch (error) {
      if (current()) this.problem = describeError(error);
      this.cdr.markForCheck();
      throw error;
    }
    try {
      const rows: Deployment[] = [];
      let cursor: string | null = null;
      let read = 0;
      let usage: ReleaseUsage;
      do {
        usage = await host.api.request<ReleaseUsage>(
          `${base}/usage?${releaseUsageQuery(cursor)}`,
        );
        if (!current()) return false;
        rows.push(...usage.deployments);
        cursor = usage.next_cursor ?? null;
      } while (cursor && ++read < this.pages);
      this.usage = usage;
      this.deployments = rows;
      this.usageCursor = cursor;
      this.usageProblem = null;
      return true;
    } catch (error) {
      if (current()) this.usageProblem = describeError(error);
      throw error;
    } finally {
      this.cdr.markForCheck();
    }
  }
  async moreUsage() {
    const host = this.host();
    const scope = environmentOf(host);
    const cursor = this.usageCursor;
    if (!scope || !cursor || this.appending || this.pages >= MAX_PAGES) return;
    const generation = this.generation;
    this.appending = true;
    try {
      const usage = await host.api.request<ReleaseUsage>(
        `${scope}/worker-releases/${encodeURIComponent(this.releaseId())}/usage?${releaseUsageQuery(cursor)}`,
      );
      if (generation !== this.generation) return;
      const seen = new Set(this.deployments.map((row) => this.rowKey(row)));
      this.deployments = [
        ...this.deployments,
        ...usage.deployments.filter((row) => !seen.has(this.rowKey(row))),
      ];
      this.usageCursor = usage.next_cursor ?? null;
      this.pages++;
    } catch (error) {
      if (generation === this.generation)
        this.usageProblem = describeError(error);
    } finally {
      if (generation === this.generation) this.appending = false;
      this.cdr.markForCheck();
    }
  }
  types() {
    return this.release
      ? releaseTaskTypes(this.release).join(", ") || "None"
      : "";
  }
  /** A state's share of every worker of the release, for its meter. */
  share(count: number) {
    const workers = this.usage?.workers;
    if (!workers) return 0;
    const total =
      workers.online + workers.offline + workers.draining + workers.revoked;
    return total ? (count / total) * 100 : 0;
  }
  path(row: Deployment) {
    return `${row.target_name} › ${row.deployment_name} › ${row.component}`;
  }
  rowKey(row: Deployment) {
    return `${row.deployment_id}/${row.component}`;
  }
}
