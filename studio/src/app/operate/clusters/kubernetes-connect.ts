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
// Clusters › Add target › Kubernetes: the commands that create and connect a
// local kind or k3d cluster, and what Studio sees while the connect command
// runs in a terminal. Studio only reads targets, then the new target's
// runners and observations; the cluster's credentials never reach it.
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  OnDestroy,
  OnInit,
  computed,
  inject,
  signal,
} from "@angular/core";
import { NgTemplateOutlet } from "@angular/common";
import { copyText } from "../../connection";
import { Icon } from "../../icon";
import { OperateState } from "../operate-state";
import { ClusterSection } from "./cluster-section";
import {
  DEFAULT_INPUT,
  PROVIDERS,
  clusterNameProblem,
  commandArguments,
  connectCommand,
  connectView,
  contextName,
  createCommand,
  detect,
  keepWaiting,
  namespaceProblem,
  observe,
  observedLine,
  startWatch,
  targetName,
  type ClusterProvider,
  type ConnectInput,
  type ConnectView,
} from "./kubernetes-connect-model";

/** The most pages of targets one look reads (50 each). */
const TARGET_PAGES = 10;
/** How long Copy reads "Copied". */
const COPIED_MS = 2000;

type Which = "create" | "connect";

@Component({
  selector: "weave-kubernetes-connect",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, NgTemplateOutlet, OperateState],
  styleUrls: ["./clusters.css", "../operate.css", "./kubernetes-connect.css"],
  template: `
    <div class="connect" (keydown.escape)="page.closeKubernetesConnect()">
      <button
        type="button"
        class="tertiary connect-back"
        (click)="page.closeKubernetesConnect()"
      >
        <weave-icon name="chevronLeft" [size]="16" />Back to Clusters
      </button>
      @if (problem(); as failed) {
        <weave-operate-state
          kind="partial"
          [message]="failed.message"
          [code]="failed.code"
          (retry)="page.poller.refresh()"
        />
      }
      <div class="connect-wizard">
        <section
          class="operations-panel connect-form"
          aria-labelledby="connect-title"
        >
          <h2 id="connect-title" tabindex="-1">
            Connect a kind or k3d cluster
          </h2>
          <div class="connect-section">
            <div class="connect-fields">
              <fieldset class="connect-provider">
                <legend>Provider</legend>
                <div class="connect-segments">
                  @for (item of providers; track item) {
                    <label class="connect-segment">
                      <input
                        type="radio"
                        name="connect-provider"
                        [value]="item"
                        [checked]="provider() === item"
                        (change)="setProvider(item)"
                      /><span>{{ item }}</span>
                    </label>
                  }
                </div>
              </fieldset>
              <div class="field">
                <label for="connect-cluster">Cluster name</label>
                <input
                  id="connect-cluster"
                  type="text"
                  autocomplete="off"
                  autocapitalize="none"
                  autocorrect="off"
                  spellcheck="false"
                  [value]="cluster()"
                  [attr.aria-invalid]="clusterProblem() ? 'true' : null"
                  [attr.aria-describedby]="
                    clusterProblem() ? 'connect-cluster-problem' : null
                  "
                  (input)="setCluster($event)"
                />
              </div>
              <div class="field">
                <label for="connect-namespace">Namespace</label>
                <input
                  id="connect-namespace"
                  type="text"
                  autocomplete="off"
                  autocapitalize="none"
                  autocorrect="off"
                  spellcheck="false"
                  [value]="namespace()"
                  [attr.aria-invalid]="namespaceProblem() ? 'true' : null"
                  [attr.aria-describedby]="
                    namespaceProblem() ? 'connect-namespace-problem' : null
                  "
                  (input)="setNamespace($event)"
                />
              </div>
              @if (clusterProblem(); as refusal) {
                <p class="connect-problem" id="connect-cluster-problem">
                  <weave-icon name="failCircle" [size]="16" /><span>{{
                    refusal
                  }}</span>
                </p>
              }
              @if (namespaceProblem(); as refusal) {
                <p class="connect-problem" id="connect-namespace-problem">
                  <weave-icon name="failCircle" [size]="16" /><span>{{
                    refusal
                  }}</span>
                </p>
              }
            </div>
            @if (name(); as target) {
              <p class="connect-target-line">
                The target will be named <code>{{ target }}</code
                >.
              </p>
            }
          </div>
          <div class="connect-section">
            <h3 id="connect-before">Before you start</h3>
            <ol class="connect-steps" aria-labelledby="connect-before">
              <li>
                <p>
                  The local platform is running (<code>weave platform up</code
                  >).
                </p>
              </li>
              <li>
                <p>
                  Create the cluster with this command. Skip it if you already
                  did.
                </p>
                <ng-container
                  [ngTemplateOutlet]="command"
                  [ngTemplateOutletContext]="{ $implicit: 'create' }"
                />
                @if (valid()) {
                  <p class="connect-says">
                    It adds the
                    <span class="connect-name">{{ context() }}</span> context to
                    your kubeconfig and makes it current. If no cluster command
                    has run on this computer yet, it first downloads about 150
                    MB of kind, k3d and kubectl into the platform folder.
                  </p>
                }
              </li>
            </ol>
          </div>
          <div class="connect-section">
            <h3>Connect</h3>
            <p class="connect-changes">
              @if (valid()) {
                Creates the namespaces
                <span class="connect-name">weave-system</span> and
                <span class="connect-name">{{ namespace() }}</span
                >, a runner and a worker deployment at 0 workers, and network
                rules that let only them reach the platform. Registers the
                target <span class="connect-name">{{ name() }}</span
                >. It also joins the platform's containers to the cluster's
                Docker network and, if no cluster command has run on this
                computer yet, downloads about 150 MB of kind, k3d and kubectl
                into the platform folder. Studio never receives your kubeconfig.
              } @else {
                Fix the {{ problemField() }} to see what connecting changes.
              }
            </p>
            <ng-container
              [ngTemplateOutlet]="command"
              [ngTemplateOutletContext]="{ $implicit: 'connect' }"
            />
          </div>
        </section>
        <aside
          class="connect-status"
          [class.success]="view() === 'connected' || view() === 'already'"
          aria-labelledby="connect-status-title"
        >
          <h3 id="connect-status-title">What Studio sees</h3>
          <div class="connect-line" role="status">
            <span class="connect-mark" [attr.data-tone]="tone()"
              ><weave-icon [name]="icon()" [size]="16"
            /></span>
            <div>
              <strong>{{ headline() }}</strong>
              @switch (view()) {
                @case ("waiting") {
                  <p>
                    Waiting for <span class="connect-name">{{ name() }}</span
                    >… Studio checks every 5 s.
                  </p>
                }
                @case ("registered") {
                  <p>
                    <span class="connect-name">{{ name() }}</span> is
                    registered. Waiting for its runner to make contact…
                  </p>
                }
                @case ("already") {
                  <p>
                    <span class="connect-name">{{ name() }}</span> is already
                    connected.
                  </p>
                }
                @case ("clash") {
                  <p>
                    A target named
                    <span class="connect-name">{{ name() }}</span> already
                    exists and isn't this cluster. Choose another cluster name
                    or namespace.
                  </p>
                }
                @case ("patience") {
                  <p>
                    Studio hasn't seen
                    <span class="connect-name">{{ name() }}</span> yet. Check
                    the command's output in your terminal.
                  </p>
                }
                @case ("paused") {
                  <p>
                    Studio starts looking for the target once the
                    {{ problemField() }} is fixed.
                  </p>
                }
              }
            </div>
          </div>
          <dl>
            <dt>Target</dt>
            @if (name(); as target) {
              <dd class="connect-mono">{{ target }}</dd>
            } @else {
              <dd class="connect-none">—</dd>
            }
            <dt>Namespace</dt>
            @if (namespaceProblem()) {
              <dd class="connect-none">—</dd>
            } @else {
              <dd class="connect-mono">{{ namespace() }}</dd>
            }
            @if (view() === "connected") {
              <dt>Runner</dt>
              <dd>
                <span class="status-pill" data-tone="success"
                  >Runner online</span
                >
              </dd>
              <dt>Deployment</dt>
              <dd>{{ observed() }}</dd>
            }
          </dl>
          @if (view() === "connected" || view() === "already") {
            <div class="connect-actions">
              <button type="button" class="primary" (click)="openConnected()">
                Open target
              </button>
              @if (view() === "connected" && canPlan()) {
                <button type="button" (click)="newChange()">
                  New change request
                </button>
              }
            </div>
          } @else if (view() === "patience") {
            <div class="connect-actions">
              <button type="button" (click)="keepWaiting()">
                Keep waiting
              </button>
            </div>
          }
        </aside>
      </div>
    </div>
    <ng-template #command let-which>
      <div
        class="connect-command"
        [class.off]="!valid()"
        role="group"
        [attr.aria-label]="
          which === 'create' ? 'Create command' : 'Connect command'
        "
      >
        @if (valid()) {
          <pre
            [id]="'connect-' + which + '-text'"
            [attr.tabindex]="selectable() === which ? 0 : null"
          >@for (part of parts(which); track $index) {<span class="connect-arg">{{ part }}</span>@if (!$last) {{{ " " }}}}</pre>
        } @else {
          <p>Fix the {{ problemField() }} to see this command.</p>
        }
        <button
          type="button"
          class="sm connect-copy"
          [class.copied]="copied() === which"
          [attr.aria-disabled]="valid() ? null : 'true'"
          (click)="copy(which, $event)"
        >
          <weave-icon
            [name]="copied() === which ? 'check' : 'copy'"
            [size]="16"
          />{{ copied() === which ? "Copied" : "Copy"
          }}<span class="sr-only"> {{ which }} command</span>
        </button>
      </div>
      <p class="connect-note" [class.refused]="note()?.refused" role="status">
        @if (note(); as shownNote) {
          @if (shownNote.which === which) {
            @if (shownNote.refused) {
              <weave-icon name="warning" [size]="16" /><span
                >Select the command and copy it.</span
              >
            } @else {
              <weave-icon name="ok" [size]="16" /><span
                >Copied. Run it in a terminal in the folder where you ran
                <code>weave platform up</code>.</span
              >
            }
          }
        }
      </p>
    </ng-template>
  `,
})
export class KubernetesConnect
  extends ClusterSection
  implements OnInit, OnDestroy
{
  private element = inject<ElementRef<HTMLElement>>(ElementRef);
  readonly providers = PROVIDERS;
  readonly provider = signal<ClusterProvider>(DEFAULT_INPUT.provider);
  readonly cluster = signal(DEFAULT_INPUT.cluster);
  readonly namespace = signal(DEFAULT_INPUT.namespace);
  readonly input = computed<ConnectInput>(() => ({
    provider: this.provider(),
    cluster: this.cluster(),
    namespace: this.namespace(),
  }));
  readonly clusterProblem = computed(() => clusterNameProblem(this.cluster()));
  readonly namespaceProblem = computed(() =>
    namespaceProblem(this.namespace()),
  );
  readonly valid = computed(
    () => !this.clusterProblem() && !this.namespaceProblem(),
  );
  /** Which field to fix first, as the refusals read: "cluster name". */
  readonly problemField = computed(() =>
    this.clusterProblem() ? "cluster name" : "namespace",
  );
  readonly name = computed(() =>
    this.valid() ? targetName(this.input()) : null,
  );
  readonly context = computed(() => contextName(this.input()));
  /** Studio's look for the current inputs. */
  private readonly watch = signal(startWatch(Date.now()));
  readonly view = computed<ConnectView | "paused">(() =>
    this.valid() ? connectView(this.watch()) : "paused",
  );
  /** "Observed 12 s ago", read with the poll that saw the target connected. */
  readonly observed = signal("Not observed yet");
  readonly copied = signal<Which | null>(null);
  readonly note = signal<{ which: Which; refused: boolean } | null>(null);
  /** The command whose text took focus because the clipboard refused it. */
  readonly selectable = signal<Which | null>(null);
  /** Moves on every change of the inputs: an answer for older inputs is dropped. */
  private generation = 0;
  private copiedTimer: ReturnType<typeof setTimeout> | null = null;

  readonly headline = computed(
    () =>
      ({
        waiting: "Waiting for the cluster",
        registered: "Registered",
        connected: "Cluster connected",
        already: "Already connected",
        clash: "Name in use",
        patience: "Not seen yet",
        paused: "Not looking yet",
      })[this.view()],
  );
  readonly icon = computed(
    () =>
      ({
        waiting: "spinner",
        registered: "spinner",
        connected: "ok",
        already: "ok",
        clash: "warning",
        patience: "stale",
        paused: "off",
      })[this.view()],
  );
  readonly tone = computed(
    () =>
      ({
        waiting: "live",
        registered: "live",
        connected: "success",
        already: "success",
        clash: "danger",
        patience: "warning",
        paused: "neutral",
      })[this.view()],
  );
  /** Why the last look failed, as the poller's partial state shows it. */
  readonly problem = () => {
    const errors = this.store.errors;
    const info = this.store.errorInfo;
    for (const name of ["targets", "runners", "observations"] as const)
      if (errors[name])
        return { message: errors[name]!, code: info[name]?.code ?? "" };
    return null;
  };

  ngOnInit() {
    this.page.connect = this;
  }
  ngOnDestroy() {
    if (this.page.connect === this) this.page.connect = null;
    if (this.copiedTimer) clearTimeout(this.copiedTimer);
  }

  parts(which: Which): string[] {
    const input = this.input();
    return commandArguments(
      which === "create" ? createCommand(input) : connectCommand(input),
    );
  }
  canPlan() {
    const target = this.watch().detection;
    return (
      "target" in target && this.host.can("deployment.plan", target.target.id)
    );
  }

  setProvider(value: ClusterProvider) {
    this.provider.set(value);
    this.restart();
  }
  setCluster(event: Event) {
    this.cluster.set((event.target as HTMLInputElement).value);
    this.restart();
  }
  setNamespace(event: Event) {
    this.namespace.set((event.target as HTMLInputElement).value);
    this.restart();
  }
  /** New inputs: detection starts again for their name. */
  private restart() {
    this.generation++;
    this.watch.set(startWatch(Date.now()));
    this.observed.set("Not observed yet");
    this.note.set(null);
    this.copied.set(null);
    this.selectable.set(null);
    // Look now: a target that appears after this answer is news.
    if (this.valid()) void this.page.poller.refresh();
  }

  /** Whether Clusters should skip its scheduled loads: patience is over. */
  paused() {
    return this.valid() && this.watch().paused;
  }
  keepWaiting() {
    this.watch.set(keepWaiting(this.watch(), Date.now()));
    void this.page.poller.refresh();
  }

  /**
   * One look for the target these inputs name: every page of targets, then
   * the matching target's runners and observations. False when a read
   * failed. An answer is applied only while the inputs and the environment
   * are still the ones it was asked for.
   */
  async poll(): Promise<boolean> {
    if (!this.valid()) return true;
    const asked = {
      generation: this.generation,
      environment: this.environment(),
      input: this.input(),
    };
    const current = () =>
      asked.generation === this.generation &&
      asked.environment === this.environment() &&
      this.page.connect === this;
    const store = this.store;
    await store.load("targets");
    for (
      let pages = 1;
      pages < TARGET_PAGES && store.cursors.targets && !store.errors.targets;
      pages++
    )
      await store.load("targets", true);
    if (store.errors.targets) return false;
    if (!current()) return true;
    const found = detect(store.targets, [], asked.input, Date.now());
    if (found.state === "registered") {
      const filter = { target_id: found.target.id };
      await Promise.all([
        store.load("runners", false, filter),
        store.load("observations", false, filter),
      ]);
      if (store.errors.runners || store.errors.observations) return false;
      if (!current()) return true;
    }
    const now = Date.now();
    const detection = detect(store.targets, store.runners, asked.input, now);
    this.watch.set(observe(this.watch(), detection, now));
    if (detection.state === "connected")
      this.observed.set(
        observedLine(store.observations, detection.target.id, now),
      );
    return true;
  }
  private environment() {
    return this.host.profile?.environmentId ?? "";
  }

  async copy(which: Which, event?: Event) {
    if (!this.valid()) return;
    const input = this.input();
    const text =
      which === "create" ? createCommand(input) : connectCommand(input);
    const generation = this.generation;
    const button = event?.currentTarget as HTMLElement | undefined;
    const copied = await copyText(text);
    // The legacy copy selects a hidden field; Copy keeps the focus.
    if (button && document.activeElement !== button && copied) button.focus();
    if (generation !== this.generation) return;
    if (this.copiedTimer) clearTimeout(this.copiedTimer);
    this.copiedTimer = null;
    if (copied) {
      this.selectable.set(null);
      this.copied.set(which);
      this.note.set({ which, refused: false });
      this.copiedTimer = setTimeout(() => {
        this.copied.set(null);
        this.copiedTimer = null;
      }, COPIED_MS);
      return;
    }
    // The clipboard refused: select the text so Ctrl or Cmd+C copies it.
    this.copied.set(null);
    this.note.set({ which, refused: true });
    this.selectable.set(which);
    const block = this.element.nativeElement.querySelector<HTMLElement>(
      `#connect-${which}-text`,
    );
    if (!block) return;
    block.tabIndex = 0;
    block.focus();
    const range = document.createRange();
    range.selectNodeContents(block);
    const selection = getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
  }

  openConnected() {
    const detection = this.watch().detection;
    if ("target" in detection) void this.page.openTarget(detection.target);
  }
  newChange() {
    const detection = this.watch().detection;
    if ("target" in detection) void this.page.openWizard(detection.target.id);
  }
}
