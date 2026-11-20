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
// Operate › Logs › Container logs: a snapshot of what a target's worker
// containers printed, which the target's runner reads. The person picks a
// target, a worker component, up to five members, how far back and how many
// lines; Fetch logs asks the runner and waits for its snapshot, and Follow
// asks again every 10 seconds while the page is visible. Every async path
// takes note of the environment, the person and a generation before its
// first await and applies its answer only while all still hold. Lines show
// through interpolation only, and copy and download as plain text.
import {
  ChangeDetectionStrategy,
  Component,
  DoCheck,
  OnDestroy,
  computed,
  input,
  signal,
} from "@angular/core";
import { ApiError } from "../../api";
import type { App } from "../../app";
import { describeError, type PlainError } from "../../errors";
import { Icon } from "../../icon";
import { scopeKeyOf } from "../operate-list";
import { OperateState } from "../operate-state";
import { Poller } from "../operate-store";
import {
  createLogRequest,
  listComponents,
  listRunners,
  listTargets,
  readLogRequestById,
  type ContainerLogsApi,
  type LogComponent,
  type LogRequestView,
  type LogRunner,
  type LogTarget,
  type MemberLog,
} from "./container-logs-api";
import {
  CONTAINER_FOLLOW_MS,
  DEFAULT_SINCE,
  DEFAULT_TAIL,
  EXPIRED_NOTE,
  MAX_MEMBERS,
  RUNNER_REFRESH_MS,
  WAIT_MS,
  defaultMembers,
  delay,
  downloadName,
  failureNote,
  fetchingNote,
  lineCount,
  lineTime,
  memberText,
  runnerStatus,
  sinceChoices,
  snapshotNote,
  tailProblem,
  waitingNote,
  type RunnerStatus,
} from "./container-logs-model";

/** How long Studio waits for a snapshot: the platform gives the runner 2 minutes. */
const WAIT_LIMIT_MS = 130_000;
/** How long "Copied …" stays before it clears. */
const COPIED_MS = 4_000;
const READ = ["deployment_log.read", "deployment.read"];

type Load = "loading" | "ready" | "error" | "refused";

@Component({
  selector: "weave-container-logs",
  standalone: true,
  // The shell's person and grants aren't signals: the tab is checked with the page.
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, OperateState],
  styleUrls: ["../operate.css", "./logs.css"],
  host: { class: "container-logs" },
  template: `@if (!readable()) {
      <p class="logs-access">
        <weave-icon name="lock" [size]="16" /><span
          >You need <code>deployment_log.read</code> in {{ environmentName() }}.
          Ask an administrator in Settings › People and access.</span
        >
      </p>
    } @else if (load() === "refused") {
      <p class="logs-access">
        <weave-icon name="lock" [size]="16" /><span
          >You need <code>deployment.read</code> on a target in
          {{ environmentName() }}. Ask an administrator in Settings › People and
          access.</span
        >
      </p>
    } @else if (load() === "loading") {
      <weave-operate-state
        kind="loading"
        noun="deployment targets"
        [rows]="3"
      />
    } @else if (load() === "error") {
      <weave-operate-state
        kind="error"
        [message]="
          'Studio could not load the targets. ' + (loadError()?.message ?? '')
        "
        [code]="loadError()?.code ?? ''"
        (retry)="refresh()"
      />
    } @else if (!targets().length) {
      <div class="logs-empty">
        <span class="badge"><weave-icon name="clusters" [size]="20" /></span>
        <h2 class="empty-title">No targets to read logs from.</h2>
        <p>
          A target's runner reads its containers' logs. Targets appear here once
          one is connected in Operate › Clusters and you may read its logs.
        </p>
      </div>
    } @else {
      <div class="container-form" role="group" aria-label="What to fetch">
        <div class="container-fields">
          <div class="field">
            <label for="container-target">Target</label>
            <select
              id="container-target"
              (change)="chooseTarget(value($event))"
            >
              @for (item of targets(); track item.id) {
                <option [value]="item.id" [selected]="item.id === targetId()">
                  {{ item.name }}
                </option>
              }
            </select>
          </div>
          <div class="field">
            <label for="container-component">Component</label>
            <select
              id="container-component"
              [attr.aria-disabled]="components().length ? null : 'true'"
              (change)="chooseComponent(value($event))"
            >
              @for (item of components(); track item.name) {
                <option
                  [value]="item.name"
                  [selected]="item.name === componentName()"
                >
                  {{ item.name }}
                </option>
              } @empty {
                <option value="" selected>No worker observed yet</option>
              }
            </select>
          </div>
          <div class="field">
            <label for="container-since">Since</label>
            <select id="container-since" (change)="since.set(+value($event))">
              @for (choice of sinceChoices; track choice.value) {
                <option
                  [value]="choice.value"
                  [selected]="choice.value === since()"
                >
                  {{ choice.label }}
                </option>
              }
            </select>
          </div>
          <div class="field">
            <label for="container-tail">Last lines</label>
            <span class="suffixed"
              ><input
                id="container-tail"
                inputmode="numeric"
                autocomplete="off"
                [value]="tail()"
                [attr.aria-invalid]="tailError() ? 'true' : null"
                [attr.aria-describedby]="
                  tailError() ? 'container-tail-problem' : 'container-tail-unit'
                "
                (input)="tail.set(value($event))"
              /><span class="suffix" id="container-tail-unit"
                >per member</span
              ></span
            >
            @if (tailError()) {
              <p class="field-error" id="container-tail-problem" role="alert">
                <weave-icon name="failCircle" [size]="16" />{{ tailError() }}
              </p>
            }
          </div>
        </div>
        <div class="members" role="group" aria-labelledby="container-members">
          <p class="members-label" id="container-members">
            Members <span class="hint">Up to {{ maxMembers }}</span>
          </p>
          <div class="chips">
            @for (member of members(); track member) {
              <span class="chip"
                ><span class="chip-text">{{ member }}</span
                ><button
                  type="button"
                  class="chip-remove"
                  [attr.aria-label]="'Remove ' + member"
                  (click)="removeMember(member)"
                >
                  <weave-icon name="close" [size]="16" /></button
              ></span>
            } @empty {
              <span class="hint">Every running member, up to 5</span>
            }
            @if (addable().length) {
              <select
                class="add-member"
                aria-label="Add a member"
                (change)="addMember($event)"
              >
                <option value="" selected>Add a member</option>
                @for (host of addable(); track host) {
                  <option [value]="host">{{ host }}</option>
                }
              </select>
            }
          </div>
        </div>
        @if (status(); as now) {
          @if (now.kind === "offline") {
            <div class="notice logs-notice" role="status">
              <p><weave-icon name="off" [size]="16" />{{ now.text }}</p>
            </div>
          } @else if (now.kind === "unsupported") {
            <div class="notice logs-notice" data-tone="info" role="status">
              <p><weave-icon name="info" [size]="16" />{{ now.text }}</p>
            </div>
          }
        }
        <div class="fetchbar">
          <button
            type="button"
            class="primary"
            [attr.aria-disabled]="fetchable() && !fetching() ? null : 'true'"
            [attr.aria-busy]="fetching() ? 'true' : null"
            (click)="fetchNow()"
          >
            @if (fetching()) {
              <weave-icon name="spinner" [size]="16" class="spin" />Fetching…
            } @else {
              Fetch logs
            }
          </button>
          <label class="checkbox-field"
            ><input
              type="checkbox"
              role="switch"
              [checked]="following()"
              [disabled]="!fetchable() && !following()"
              (change)="setFollow(checked($event))"
            />Follow</label
          >
          <p class="hint" role="status">
            {{ fetching() ? waiting() : fetchingNote(targetName()) }}
          </p>
        </div>
      </div>
      @if (fetchError(); as error) {
        <div class="notice logs-notice" data-tone="danger" role="alert">
          <p>{{ error.message }}</p>
          @if (error.code) {
            <small class="support-code">Support code: {{ error.code }}</small>
          }
        </div>
      }
      @if (view(); as snapshot) {
        @if (expired(snapshot)) {
          <div class="notice logs-notice" data-tone="info">
            <p><weave-icon name="stale" [size]="16" />{{ expiredNote }}</p>
            <button
              type="button"
              class="sm"
              [attr.aria-disabled]="fetchable() ? null : 'true'"
              (click)="fetchNow()"
            >
              Fetch logs
            </button>
          </div>
        } @else if (snapshot.state === "failed" && snapshot.failure) {
          <div class="notice logs-notice" data-tone="danger" role="alert">
            <p>
              <weave-icon name="failCircle" [size]="16" />{{
                failure(snapshot)
              }}
            </p>
          </div>
        } @else if (snapshot.state === "ready" && snapshot.readAt) {
          <p class="hint snapshot-note">
            {{ note(snapshot.readAt) }}
            @if (snapshot.truncated) {
              Long lines were cut, and older lines or members left out, to keep
              the snapshot within 256 KiB.
            }
          </p>
          <!-- Seen and announced alike, and cleared after a few seconds so no stale "Copied" stays. -->
          <p class="hint copied" role="status">{{ copied() }}</p>
          @for (member of snapshot.members; track member.member) {
            <section class="member" [attr.aria-label]="member.member">
              <div class="member-head">
                <h3>{{ member.member }}</h3>
                <span class="count">{{ count(member.lines.length) }}</span>
                <span class="member-actions">
                  <button
                    type="button"
                    class="sm"
                    [attr.aria-label]="'Copy the lines of ' + member.member"
                    (click)="copy(member)"
                  >
                    <weave-icon name="copy" [size]="16" />Copy
                  </button>
                  <button
                    type="button"
                    class="sm"
                    [attr.aria-label]="'Download the lines of ' + member.member"
                    (click)="download(snapshot, member)"
                  >
                    <weave-icon name="download" [size]="16" />Download
                  </button>
                </span>
              </div>
              @if (member.lines.length) {
                <div
                  class="member-lines"
                  role="table"
                  [attr.aria-label]="'Lines of ' + member.member"
                >
                  @for (line of member.lines; track $index) {
                    <div class="line" role="row">
                      <span role="cell" class="t"
                        ><time [attr.datetime]="line.at">{{
                          time(line.at)
                        }}</time></span
                      ><span
                        role="cell"
                        class="stream"
                        [attr.data-stream]="line.stream"
                        >{{ line.stream }}</span
                      ><span role="cell" class="text">{{ line.text }}</span>
                    </div>
                  }
                </div>
              } @else {
                <p class="hint member-empty">Nothing printed in this range.</p>
              }
            </section>
          } @empty {
            <p class="hint">
              None of the members printed anything in this range.
            </p>
          }
        }
      }
    }`,
})
export class ContainerLogs implements DoCheck, OnDestroy {
  readonly host = input.required<App>();
  readonly sinceChoices = sinceChoices;
  readonly maxMembers = MAX_MEMBERS;
  readonly expiredNote = EXPIRED_NOTE;
  readonly fetchingNote = fetchingNote;
  readonly load = signal<Load>("loading");
  readonly loadError = signal<PlainError | null>(null);
  readonly targets = signal<LogTarget[]>([]);
  readonly runners = signal<LogRunner[]>([]);
  readonly components = signal<LogComponent[]>([]);
  readonly targetId = signal("");
  readonly componentName = signal("");
  readonly since = signal(DEFAULT_SINCE);
  readonly tail = signal(String(DEFAULT_TAIL));
  readonly members = signal<string[]>([]);
  readonly view = signal<LogRequestView | null>(null);
  readonly fetching = signal(false);
  readonly fetchError = signal<PlainError | null>(null);
  readonly following = signal(false);
  readonly copied = signal("");
  /** When the targets, runners and components last loaded (epoch milliseconds). */
  readonly updatedAt = signal<number | null>(null);
  readonly busy = signal(false);
  /** Follow: a new snapshot every 10 s while the page is visible. */
  readonly follower = new Poller(() => this.followFetch(), CONTAINER_FOLLOW_MS);
  /**
   * The target's runners, read again every 30 s while the tab shows and after
   * each fetch: a runner is in touch for 90 s after its last contact, so a
   * list read once would turn it offline while it still answers.
   */
  readonly runnerWatch = new Poller(
    () => this.readRunners(),
    RUNNER_REFRESH_MS,
  );
  private scopeKey = "";
  /** Bumped by every change of what is shown, so a late answer is dropped. */
  private generation = 0;
  private wait: { cancel(): void } | null = null;
  private copiedTimer: ReturnType<typeof setTimeout> | null = null;

  readonly target = computed(
    () => this.targets().find((item) => item.id === this.targetId()) ?? null,
  );
  readonly targetName = computed(() => this.target()?.name ?? "the target");
  readonly tailError = computed(() => tailProblem(this.tail()));
  /** Whether the target's runner can fetch logs now: it is in touch within 90 s. */
  status(): RunnerStatus | null {
    const target = this.target();
    return target ? runnerStatus(target, this.runners(), Date.now()) : null;
  }
  fetchable() {
    return (
      this.status()?.kind === "online" &&
      !!this.componentName() &&
      !this.tailError()
    );
  }
  readonly addable = computed(() => {
    const chosen = new Set(this.members());
    if (chosen.size >= MAX_MEMBERS) return [];
    const component = this.components().find(
      (item) => item.name === this.componentName(),
    );
    return (component?.members ?? []).filter((host) => !chosen.has(host));
  });
  readonly waiting = computed(() => waitingNote(this.targetName()));

  /** With deployment_log.read and deployment.read somewhere in the workspace. */
  readable() {
    const host = this.host();
    return READ.every((capability) => host.canAnywhere(capability));
  }
  ngDoCheck() {
    const host = this.host();
    const key = scopeKeyOf(host);
    if (key === this.scopeKey) return;
    // Another platform, environment or person: nothing read before stays.
    this.scopeKey = key;
    this.forget();
    if (host.profile && host.identity && !host.signInEnded && this.readable())
      void this.refresh();
  }
  ngOnDestroy() {
    this.forget();
  }
  private forget() {
    this.generation++;
    this.follower.stop();
    this.runnerWatch.stop();
    this.following.set(false);
    this.clearWait();
    this.load.set("loading");
    this.targets.set([]);
    this.runners.set([]);
    this.components.set([]);
    this.view.set(null);
    this.fetching.set(false);
    this.fetchError.set(null);
    this.copied.set("");
  }
  private clearWait() {
    // Ending the wait lets the fetch awaiting it see that it no longer applies.
    this.wait?.cancel();
    this.wait = null;
    if (this.copiedTimer) clearTimeout(this.copiedTimer);
    this.copiedTimer = null;
  }
  private api(): ContainerLogsApi | null {
    try {
      const api = this.host().api;
      return api.environment ? api : null;
    } catch {
      return null;
    }
  }
  /** Reads the targets, then the chosen target's runners and components. */
  async refresh(): Promise<void> {
    const api = this.api();
    if (!api) return;
    const generation = this.generation;
    const scope = this.scopeKey;
    const current = () =>
      generation === this.generation && scope === this.scopeKey;
    this.busy.set(true);
    try {
      const host = this.host();
      const targets = (await listTargets(api)).filter(
        (item) =>
          !item.disabled &&
          READ.every((capability) => host.can(capability, item.id)),
      );
      if (!current()) return;
      this.targets.set(targets);
      const kept = targets.find((item) => item.id === this.targetId());
      const chosen = kept ?? targets[0] ?? null;
      if (!chosen) {
        this.targetId.set("");
        this.load.set("ready");
        this.updatedAt.set(Date.now());
        return;
      }
      await this.readTarget(api, chosen.id, !kept, current);
      if (!current()) return;
      this.load.set("ready");
      this.loadError.set(null);
      this.updatedAt.set(Date.now());
      this.runnerWatch.start(false);
    } catch (error) {
      if (!current()) return;
      const plain = describeError(error);
      this.loadError.set(plain);
      this.load.set(plain.status === 403 ? "refused" : "error");
      this.signInGone(error);
    } finally {
      if (current()) this.busy.set(false);
    }
  }
  /** The target's runners and components; `fresh` starts its choices over. */
  private async readTarget(
    api: ContainerLogsApi,
    targetId: string,
    fresh: boolean,
    current: () => boolean,
  ) {
    const [runners, components] = await Promise.all([
      listRunners(api, targetId),
      listComponents(api, targetId),
    ]);
    if (!current()) return;
    this.targetId.set(targetId);
    this.runners.set(runners);
    this.components.set(components);
    const keep = components.find((item) => item.name === this.componentName());
    if (fresh || !keep) {
      this.componentName.set(components[0]?.name ?? "");
      this.members.set(defaultMembers(components[0] ?? null));
    }
  }
  /** The chosen target's runners again; false when there is nothing to read. */
  private async readRunners(): Promise<boolean> {
    const api = this.api();
    const targetId = this.targetId();
    if (!api || !targetId) return false;
    const scope = this.scopeKey;
    try {
      const runners = await listRunners(api, targetId);
      // Fetches don't change the runners, so only another target or scope drops the answer.
      if (scope !== this.scopeKey || targetId !== this.targetId()) return false;
      this.runners.set(runners);
      return true;
    } catch (error) {
      if (scope === this.scopeKey) this.signInGone(error);
      throw error;
    }
  }
  chooseTarget(id: string) {
    if (id === this.targetId()) return;
    this.generation++;
    this.stopFollowing();
    this.clearWait();
    this.view.set(null);
    this.fetching.set(false);
    this.fetchError.set(null);
    this.targetId.set(id);
    this.runners.set([]);
    this.components.set([]);
    this.componentName.set("");
    this.members.set([]);
    const api = this.api();
    if (!api) return;
    const generation = this.generation;
    const scope = this.scopeKey;
    const current = () =>
      generation === this.generation && scope === this.scopeKey;
    this.busy.set(true);
    this.readTarget(api, id, true, current)
      .then(() => current() && this.updatedAt.set(Date.now()))
      .catch((error) => {
        if (!current()) return;
        this.fetchError.set(describeError(error));
        this.signInGone(error);
      })
      .finally(() => current() && this.busy.set(false));
  }
  chooseComponent(name: string) {
    if (name === this.componentName()) return;
    this.componentName.set(name);
    this.members.set(
      defaultMembers(
        this.components().find((item) => item.name === name) ?? null,
      ),
    );
  }
  removeMember(host: string) {
    this.members.set(this.members().filter((item) => item !== host));
  }
  addMember(event: Event) {
    const select = event.target as HTMLSelectElement;
    const host = select.value;
    select.value = "";
    if (
      host &&
      this.members().length < MAX_MEMBERS &&
      !this.members().includes(host)
    )
      this.members.set([...this.members(), host]);
  }
  fetchNow() {
    if (!this.fetchable() || this.fetching()) return;
    void this.fetch();
  }
  setFollow(on: boolean) {
    this.following.set(on);
    if (on) this.follower.start(true);
    else this.follower.stop();
  }
  private stopFollowing() {
    this.follower.stop();
    this.following.set(false);
  }
  private async followFetch(): Promise<boolean> {
    // Back to every 10 s after a wait the platform asked for.
    this.follower.setInterval(CONTAINER_FOLLOW_MS);
    if (!this.fetchable()) {
      this.stopFollowing();
      return false;
    }
    if (this.fetching()) return false;
    return (await this.fetch()) === "applied";
  }
  /** Asks the runner for a snapshot, then waits for it; the answer applies only to what was asked. */
  private async fetch(): Promise<"applied" | "dropped" | "failed"> {
    const api = this.api();
    const target = this.target();
    if (!api || !target) return "dropped";
    this.generation++;
    this.clearWait();
    const generation = this.generation;
    const scope = this.scopeKey;
    const current = () =>
      generation === this.generation && scope === this.scopeKey;
    this.fetching.set(true);
    this.fetchError.set(null);
    this.copied.set("");
    try {
      let snapshot = await createLogRequest(
        api,
        {
          target_id: target.id,
          component: this.componentName(),
          members: this.members(),
          since_seconds: this.since(),
          tail_lines: Number(this.tail().trim()),
        },
        crypto.randomUUID(),
      );
      const deadline = Date.now() + WAIT_LIMIT_MS;
      while (
        current() &&
        snapshot.state === "pending" &&
        Date.now() < deadline
      ) {
        const wait = delay(WAIT_MS);
        this.wait = wait;
        await wait.done;
        if (!current()) return "dropped";
        snapshot = await readLogRequestById(api, snapshot.id);
      }
      if (!current()) return "dropped";
      this.view.set(snapshot);
      // The runner answered, so it is in touch: what the page says about it is read again.
      void this.runnerWatch.refresh();
      return "applied";
    } catch (error) {
      if (!current()) return "dropped";
      const plain = describeError(error);
      this.fetchError.set(plain);
      // Follow asks again no sooner than the platform said it may.
      if (
        plain.status === 429 &&
        error instanceof ApiError &&
        error.retryAfter !== undefined
      )
        this.follower.setInterval(
          Math.max(CONTAINER_FOLLOW_MS, error.retryAfter * 1000),
        );
      // The runner's state may have changed: what the page says about it is read again.
      if (
        plain.code === "WV-RUNNER-OFFLINE" ||
        plain.code === "WV-DEPLOYMENT-UNSUPPORTED"
      )
        await this.readTarget(api, target.id, false, current).catch(
          () => undefined,
        );
      if (plain.status === 403 || plain.code === "WV-RUNNER-OFFLINE")
        this.stopFollowing();
      this.signInGone(error);
      return "failed";
    } finally {
      if (current()) this.fetching.set(false);
    }
  }
  expired(snapshot: LogRequestView) {
    return (
      snapshot.state === "expired" ||
      Date.now() >= Date.parse(snapshot.expiresAt)
    );
  }
  failure(snapshot: LogRequestView) {
    return snapshot.failure
      ? failureNote(snapshot.failure, this.targetName(), snapshot.component)
      : "";
  }
  note(readAt: string) {
    return snapshotNote(readAt);
  }
  count(lines: number) {
    return lineCount(lines);
  }
  time(at: string) {
    return lineTime(at);
  }
  async copy(member: MemberLog) {
    if (this.copiedTimer) clearTimeout(this.copiedTimer);
    try {
      await navigator.clipboard.writeText(memberText(member));
      this.copied.set(
        `Copied ${lineCount(member.lines.length)} of ${member.member}.`,
      );
    } catch {
      this.copied.set(
        "Studio couldn't copy the lines. Select them and copy them instead.",
      );
    }
    this.copiedTimer = setTimeout(() => this.copied.set(""), COPIED_MS);
  }
  download(snapshot: LogRequestView, member: MemberLog) {
    const blob = new Blob([memberText(member)], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = downloadName(
      this.targetName(),
      snapshot.component,
      member.member,
      snapshot.readAt ?? snapshot.createdAt,
    );
    link.click();
    setTimeout(() => URL.revokeObjectURL(url));
  }
  environmentName() {
    return this.host().workspaceNames?.environment || "this environment";
  }
  /** An ended sign-in is the shell's to explain, with its own Sign in again. */
  private signInGone(error: unknown) {
    const plain = describeError(error);
    if (plain.status === 401 && !plain.code.startsWith("WV-STUDIO-"))
      this.host().fail(error);
  }
  value(event: Event) {
    return (event.target as HTMLInputElement | HTMLSelectElement).value;
  }
  checked(event: Event) {
    return (event.target as HTMLInputElement).checked;
  }
}
