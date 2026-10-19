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
// Operate › Workers › Releases: the worker releases admitted in this
// environment (releases.list) and, for the one open, what runs it. Admitting a
// release stays a CLI step; the page shows the exact command and never posts
// one. Reloads every 10 s while visible.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  DoCheck,
  Input,
  OnDestroy,
  OnInit,
  inject,
} from "@angular/core";
import { NavigationEnd, Router } from "@angular/router";
import type { App } from "../../app";
import { copyText } from "../../connection";
import type { PlainError } from "../../errors";
import { shortId } from "../../format";
import { Icon } from "../../icon";
import { ModalSheet, sheetWhen } from "../../modal-sheet";
import type { Page } from "../../operations/deployment-contracts";
import { MAX_PAGES } from "../operate-limits";
import {
  ListReads,
  environmentOf,
  listFailure,
  scopeKeyOf,
} from "../operate-list";
import { OperateState } from "../operate-state";
import { releasesPath, viewPath, workersRoute } from "../operate-routes";
import { Poller } from "../operate-store";
import { RefreshStatus } from "../refresh-status";
import { ReleaseDetail } from "./release-detail";
import {
  releaseAdmissionCommand,
  releaseTaskTypes,
  shortDigest,
  type WorkerRelease,
} from "./workers-model";

@Component({
  selector: "weave-releases-page",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, ModalSheet, OperateState, RefreshStatus, ReleaseDetail],
  styleUrls: ["../operate.css", "./workers.css"],
  template: `
    @if (!host.identity) {
      <weave-operate-state kind="loading" noun="releases" />
    } @else if (!readable || forbidden) {
      <weave-operate-state kind="access" capability="catalog.read" />
    } @else if (!loaded && error) {
      <weave-operate-state
        kind="error"
        [message]="error.message"
        [code]="error.code"
        (retry)="poller.refresh()"
      />
    } @else if (!loaded) {
      <weave-operate-state kind="loading" noun="releases" />
    } @else {
      <div class="releases-bar">
        <span class="operate-count" role="status">{{ countText() }}</span>
        <weave-refresh-status
          [updatedAt]="poller.lastSuccessAt"
          [busy]="poller.busy"
          (refresh)="poller.refresh()"
        />
      </div>
      @if (error) {
        <weave-operate-state
          kind="partial"
          [message]="error.message"
          [code]="error.code"
          (retry)="poller.refresh()"
        />
      }
      <div class="resource-layout" [class.has-detail]="!!openId">
        <div class="release-list">
          @if (!releases.length) {
            <weave-operate-state
              kind="empty"
              heading="No releases yet"
              text="A worker release appears here once it is admitted with the CLI command below."
            />
          } @else {
            <div
              class="resource-table release-table"
              role="table"
              aria-label="Releases"
            >
              <div role="rowgroup">
                <div class="table-head" role="row">
                  <span role="columnheader">Release</span
                  ><span role="columnheader">Task types</span
                  ><span role="columnheader" data-wide>Release ID</span>
                </div>
              </div>
              <div role="rowgroup">
                @for (release of releases; track release.id) {
                  <div
                    class="resource-row"
                    role="row"
                    [class.selected]="openId === release.id"
                  >
                    <span role="cell" class="row-text"
                      ><button
                        type="button"
                        class="row-open"
                        [attr.aria-current]="
                          openId === release.id ? 'true' : null
                        "
                        [attr.title]="release.image_digest"
                        (click)="open(release.id)"
                      >
                        {{ digest(release.image_digest) }}</button
                      ><small>{{ typesCount(release) }}</small></span
                    ><span
                      role="cell"
                      class="cell-text"
                      [attr.title]="typesText(release)"
                      >{{ typesText(release) }}</span
                    ><span role="cell" data-wide
                      ><code [attr.title]="release.id">{{
                        short(release.id)
                      }}</code></span
                    >
                  </div>
                }
              </div>
            </div>
            @if (canLoadMore) {
              <button
                type="button"
                class="load-more"
                [disabled]="appending"
                (click)="loadMore()"
              >
                Load more releases
              </button>
            } @else if (nextCursor) {
              <p class="list-limit" role="status">
                Showing the first 500 releases.
              </p>
            }
          }
          <section
            class="release-admission"
            aria-labelledby="release-admission-title"
          >
            <h2 id="release-admission-title">Admit a release</h2>
            <p class="hint">
              Studio doesn't admit releases. Put the release request (its image
              digest and capabilities) in <code>release.json</code> and run:
            </p>
            <div class="command-line">
              <code>{{ command }}</code
              ><button
                type="button"
                class="icon-button"
                aria-label="Copy the command"
                (click)="copyCommand()"
              >
                <weave-icon name="copy" [size]="16" />
              </button>
            </div>
          </section>
        </div>
        @if (openId) {
          <section
            class="record-detail"
            role="complementary"
            aria-labelledby="release-detail-title"
            [weaveModalSheet]="true"
            [sheetWhen]="sheetWhen.detail"
            sheetInitialFocus="#release-detail-title"
            (sheetDismiss)="close()"
          >
            <weave-release-detail
              [host]="host"
              [releaseId]="openId"
              (showWorkers)="showWorkers($event)"
              (closed)="close()"
            />
          </section>
        }
      </div>
    }
  `,
})
export class ReleasesPage implements DoCheck, OnInit, OnDestroy {
  @Input({ required: true }) host!: App;
  private cdr = inject(ChangeDetectorRef);
  private router = inject(Router);
  private navigated = this.router.events.subscribe((event) => {
    if (event instanceof NavigationEnd)
      this.openId = workersRoute(location.pathname).release;
  });
  readonly sheetWhen = sheetWhen;
  readonly short = shortId;
  readonly digest = shortDigest;
  readonly command = releaseAdmissionCommand;
  readonly poller = new Poller(() => this.load(), 10_000);
  releases: WorkerRelease[] = [];
  nextCursor: string | null = null;
  loaded = false;
  forbidden = false;
  error: PlainError | null = null;
  appending = false;
  /** The release whose detail is open ("/operate/workers/releases/ID"). */
  openId = "";
  private pages = 1;
  private readonly listReads = new ListReads();
  private scopeKey = "";
  private generation = 0;

  get readable() {
    return this.host.canAnywhere("catalog.read");
  }
  get canLoadMore() {
    return !!this.nextCursor && this.pages < MAX_PAGES;
  }
  private get scope() {
    return environmentOf(this.host);
  }
  ngOnInit() {
    this.openId = workersRoute(location.pathname).release;
    this.poller.start(false);
  }
  ngOnDestroy() {
    this.navigated.unsubscribe();
    this.poller.stop();
    this.generation++;
  }
  ngDoCheck() {
    const key = scopeKeyOf(this.host);
    if (key === this.scopeKey) return;
    this.scopeKey = key;
    this.generation++;
    this.releases = [];
    this.nextCursor = null;
    this.loaded = false;
    this.forbidden = false;
    this.error = null;
    this.pages = 1;
    this.appending = false;
    if (this.scope && this.host.identity && this.readable)
      void this.poller.refresh();
  }
  private async load() {
    const scope = this.scope;
    if (!scope || !this.host.identity || !this.readable) return false;
    const generation = this.generation;
    return this.listReads.run(async () => {
      if (generation !== this.generation) return false;
      try {
        const items: WorkerRelease[] = [];
        let cursor: string | null = null;
        let read = 0;
        do {
          const query = new URLSearchParams({ limit: "50" });
          if (cursor) query.set("cursor", cursor);
          const page: Page<WorkerRelease> = await this.host.api.request(
            `${scope}/worker-releases?${query}`,
          );
          if (generation !== this.generation) return false;
          items.push(...page.items);
          cursor = page.next_cursor;
        } while (cursor && ++read < this.pages);
        this.releases = items;
        this.nextCursor = cursor;
        this.loaded = true;
        this.forbidden = false;
        this.error = null;
        return true;
      } catch (error) {
        if (generation !== this.generation) return false;
        ({ error: this.error, forbidden: this.forbidden } = listFailure(error));
        throw error;
      } finally {
        this.cdr.markForCheck();
      }
    });
  }
  async loadMore() {
    const scope = this.scope;
    if (!scope || !this.canLoadMore || this.appending) return;
    const generation = this.generation;
    this.appending = true;
    try {
      await this.listReads.run(async () => {
        if (generation !== this.generation || !this.canLoadMore) return;
        const query = new URLSearchParams({
          limit: "50",
          cursor: this.nextCursor!,
        });
        const page: Page<WorkerRelease> = await this.host.api.request(
          `${scope}/worker-releases?${query}`,
        );
        if (generation !== this.generation) return;
        const seen = new Set(this.releases.map((item) => item.id));
        this.releases = [
          ...this.releases,
          ...page.items.filter((item) => !seen.has(item.id)),
        ];
        this.nextCursor = page.next_cursor;
        this.pages++;
        this.error = null;
      });
    } catch (error) {
      if (generation === this.generation)
        ({ error: this.error, forbidden: this.forbidden } = listFailure(error));
    } finally {
      if (generation === this.generation) this.appending = false;
      this.cdr.markForCheck();
    }
  }
  open(id: string) {
    this.openId = id;
    void this.router.navigateByUrl(releasesPath(id));
  }
  close() {
    this.openId = "";
    void this.router.navigateByUrl(releasesPath());
  }
  /** The fleet, narrowed to the workers that run this release. */
  showWorkers(id: string) {
    void this.router.navigateByUrl(
      `${viewPath("workers")}?release=${encodeURIComponent(id)}`,
    );
  }
  async copyCommand() {
    if (await copyText(this.command)) this.host.notify("Copied the command.");
    else
      this.host.notify("Studio couldn't copy. Select the command and copy it.");
  }
  countText() {
    const count = this.releases.length;
    const noun = count === 1 ? "release" : "releases";
    return this.nextCursor ? `${count} ${noun} loaded` : `${count} ${noun}`;
  }
  typesText(release: WorkerRelease) {
    return releaseTaskTypes(release).join(", ") || "None";
  }
  typesCount(release: WorkerRelease) {
    const count = release.capabilities?.length ?? 0;
    return `${count} task ${count === 1 ? "type" : "types"}`;
  }
}
