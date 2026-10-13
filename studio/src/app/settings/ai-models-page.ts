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
// Settings › AI models: one place to connect models, check that AI steps can
// run, and set up Weave AI. The shell owns the AI connection sheet; this page
// asks it to open and reloads whenever an AI connection or test changes. The
// platform checks every permission; this page only offers what a person may do.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  DoCheck,
  ElementRef,
  OnDestroy,
  inject,
  input,
} from "@angular/core";
import { ApiError, type Profile, type StudioApi } from "../api";
import { describeError } from "../errors";
import { Icon } from "../icon";
import { RowMenu, type RowMenuItem } from "../row-menu";
import type {
  AiSheetRequest,
  AiTestRecord,
} from "../integrations/ai-connection-sheet";
import type { AiConnection } from "../integrations/ai-provider-connection";
import {
  PROVIDER_CARDS,
  allReady,
  cardInfo,
  cardOf,
  cardOfBindable,
  endpointFor,
  fixAction,
  keylessConnection,
  readinessRows,
  type ProviderCard,
  type ReadinessRow,
} from "../integrations/ai-models-model";
import {
  AiSetupApi,
  type AiEndpoint,
  type BindableConnection,
  type Readiness,
} from "../integrations/ai-setup-api";
import { AiReadinessList } from "./ai-readiness-list";

/** What the page reads from and asks of the shell. */
export interface AiModelsHost {
  readonly api: StudioApi;
  readonly profile: Profile | null;
  readonly aiSetupGeneration: number;
  readonly aiTests: ReadonlyMap<string, AiTestRecord>;
  can(capability: string, resource?: string): boolean;
  roleLabel(role: string): string;
  openAiConnectionDialog(request?: AiSheetRequest): void;
  navigate(view: "workers"): Promise<void>;
  notify(text: string): void;
}
/** A connection row: managed connections can be tested; chosen ones only viewed. */
interface Row {
  connection: AiConnection;
  card: ProviderCard;
  readOnly: boolean;
}
const fromBindable = (item: BindableConnection): AiConnection => ({
  id: item.revision_id,
  name: item.name,
  revision: 0,
  connector: item.connector,
  config: {
    provider: item.provider ?? "openai-chat",
    endpoint: item.endpoint_origin ?? "",
  },
});

@Component({
  selector: "weave-ai-models-page",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, RowMenu, AiReadinessList],
  styleUrl: "./ai-models-page.css",
  template: `<p class="ai-intro">
      Connect a model once. Workflows and Weave AI use the connections you set
      up here.
    </p>
    <div class="ai-layout">
      <section
        class="ai-panel ai-readiness"
        aria-labelledby="ai-readiness-title"
      >
        <div class="panel-heading">
          <h2 id="ai-readiness-title" tabindex="-1">Readiness</h2>
          <button
            type="button"
            class="tertiary"
            [attr.aria-disabled]="checking ? 'true' : null"
            (click)="!checking && recheck()"
          >
            <weave-icon name="refresh" [size]="16" />Recheck
          </button>
        </div>
        @if (readinessError) {
          <p class="notice" data-tone="danger" role="alert">
            {{ readinessError }}
          </p>
        } @else if (!readiness) {
          <p role="status">Checking…</p>
        } @else {
          @if (ready) {
            <p class="notice" data-tone="success" role="status">
              AI steps can run in this environment.
            </p>
          }
          <weave-ai-readiness-list
            [rows]="rows"
            [actions]="actions"
            [running]="running"
            [failures]="failures"
            (act)="act($event)"
            (copy)="copy($event)"
          />
        }
      </section>
      <section
        class="ai-panel ai-providers"
        aria-labelledby="ai-providers-title"
      >
        <div class="panel-heading">
          <h2 id="ai-providers-title" tabindex="-1">Providers</h2>
        </div>
        <ul class="provider-cards">
          @for (card of cards; track card.id) {
            @let total = count(card.id);
            @let action = cardAction(card.id);
            <li class="provider-card">
              <p class="provider-title">
                <weave-icon [name]="card.icon" [size]="16" />{{ card.title }}
              </p>
              <p class="provider-kind">{{ card.kind }}</p>
              <p class="provider-state">
                @if (total) {
                  <span class="pill" data-tone="success"
                    >Connected · {{ total }}</span
                  >
                } @else {
                  <span class="pill">Not set up</span>
                }
              </p>
              @if (action) {
                <button
                  type="button"
                  [attr.aria-label]="action + ' ' + card.title"
                  (click)="openCard(card.id)"
                >
                  {{ action }}
                </button>
              }
            </li>
          }
        </ul>
        <h3 class="connections-heading" id="ai-connections-title">
          Connections
        </h3>
        @if (listError) {
          <p class="notice" data-tone="danger" role="alert">{{ listError }}</p>
        } @else if (!canManage() && !canBind()) {
          <p class="empty">Seeing AI connections requires the Deployer role.</p>
        } @else {
          <ul class="connection-list" aria-labelledby="ai-connections-title">
            @for (item of visible; track item.connection.id) {
              <li class="connection-row">
                <div class="connection-main">
                  <span class="connection-name">{{
                    item.connection.name
                  }}</span>
                  <span class="connection-meta">
                    <span>{{ providerLabel(item) }}</span>
                    <span class="endpoint">{{
                      item.connection.config.endpoint
                    }}</span>
                    @if (developmentOnly(item)) {
                      <span class="pill">Development only</span>
                    }
                  </span>
                  @if (!item.readOnly) {
                    <span
                      class="connection-test"
                      [attr.data-tone]="testTone(item)"
                      >{{ testText(item) }}</span
                    >
                  }
                </div>
                <div class="connection-actions">
                  @if (item.readOnly) {
                    <button
                      type="button"
                      [attr.aria-label]="'View ' + item.connection.name"
                      (click)="openRow(item)"
                    >
                      View
                    </button>
                  } @else {
                    <button
                      type="button"
                      [attr.aria-label]="'Test ' + item.connection.name"
                      (click)="openRow(item, 'model')"
                    >
                      Test
                    </button>
                    <weave-row-menu
                      [label]="'More actions for ' + item.connection.name"
                      [items]="menu(item)"
                    />
                  }
                </div>
              </li>
            } @empty {
              <li class="empty">
                {{
                  listed
                    ? "No AI connections yet. Connect a provider above."
                    : "Loading…"
                }}
              </li>
            }
          </ul>
        }
      </section>
    </div>
    <p class="sr-only" role="status" aria-live="polite">{{ announcement }}</p>`,
})
export class AiModelsPage implements DoCheck, OnDestroy {
  host = input.required<AiModelsHost>();
  readonly cards = PROVIDER_CARDS;
  private readonly cdr = inject(ChangeDetectorRef);
  private readonly element = inject<ElementRef<HTMLElement>>(ElementRef);
  /** The AI setup generation and the person's access the page last loaded for. */
  private loadedFor = "";
  private loads = 0;
  private alive = true;
  private setup: AiSetupApi | null = null;
  readiness: Readiness | null = null;
  readinessError = "";
  checking = false;
  endpoints: AiEndpoint[] = [];
  connections: AiConnection[] = [];
  bindable: BindableConnection[] | null = null;
  listed = false;
  listError = "";
  running = "";
  failures: Record<string, string> = {};
  announcement = "";

  ngDoCheck() {
    // Capabilities arrive after the page may first render; what the page may
    // list depends on them, so a change in access reloads it too.
    const key = `${this.host().aiSetupGeneration}:${this.canManage()}:${this.canBind()}`;
    if (key === this.loadedFor) return;
    this.loadedFor = key;
    void this.load();
  }
  ngOnDestroy() {
    this.alive = false;
  }
  canManage() {
    return this.host().can("connection.manage");
  }
  canBind() {
    return this.host().can("connection.bind");
  }
  async load() {
    const load = ++this.loads;
    this.setup = new AiSetupApi(this.host().api);
    await Promise.all([this.recheck(load), this.loadConnections(load)]);
  }
  async recheck(load = this.loads) {
    const setup = this.setup;
    if (!setup) return;
    this.checking = true;
    try {
      const readiness = await setup.readiness();
      if (this.alive && load === this.loads) {
        this.readiness = readiness;
        this.readinessError = "";
      }
    } catch (error) {
      if (this.alive && load === this.loads)
        this.readinessError =
          error instanceof ApiError && error.status === 403
            ? "Checking AI readiness requires the Viewer role."
            : describeError(error).message;
    } finally {
      if (this.alive) {
        this.checking = false;
        this.cdr.markForCheck();
      }
    }
  }
  private async loadConnections(load: number) {
    const setup = this.setup!;
    try {
      const endpoints = await setup
        .endpoints()
        .catch(() => ({ policy: "absent" as const, endpoints: [] }));
      const connections = this.canManage() ? await setup.connections() : [];
      const bindable =
        !this.canManage() && this.canBind() ? await setup.bindable() : null;
      if (!this.alive || load !== this.loads) return;
      this.endpoints = endpoints.endpoints ?? [];
      this.connections = connections;
      this.bindable = bindable;
      this.listError = "";
      this.listed = true;
    } catch (error) {
      if (this.alive && load === this.loads)
        this.listError = describeError(error).message;
    } finally {
      if (this.alive) this.cdr.markForCheck();
    }
  }
  get visible(): Row[] {
    if (this.bindable)
      return this.bindable.map((item) => ({
        connection: fromBindable(item),
        card: cardOfBindable(item, this.endpoints),
        readOnly: true,
      }));
    return this.connections.map((connection) => ({
      connection,
      card: cardOf(connection, this.endpoints),
      readOnly: false,
    }));
  }
  get ready() {
    return !!this.readiness && allReady(this.readiness);
  }
  get rows(): ReadinessRow[] {
    if (!this.readiness) return [];
    const latest = [...this.host().aiTests.values()]
      .filter(
        (record) =>
          record.result.ok &&
          this.connections.some((c) => c.id === record.revision),
      )
      .sort((a, b) => b.at - a.at)[0];
    return readinessRows(
      this.readiness,
      (role) => this.host().roleLabel(role),
      latest?.result,
    );
  }
  get actions() {
    const host = this.host();
    return this.rows.map((row) =>
      fixAction(row.fix, (capability) => host.can(capability)),
    );
  }
  count(card: ProviderCard) {
    return this.visible.filter((row) => row.card === card).length;
  }
  cardAction(card: ProviderCard): string {
    if (this.canManage()) return this.count(card) ? "Manage" : "Connect";
    return this.count(card) ? "View" : "";
  }
  providerLabel(row: Row) {
    const title = cardInfo(row.card).title;
    const provider = row.connection.config.provider;
    return row.card === "openai" || row.card === "azure"
      ? `${title} · ${provider.endsWith("-chat") ? "Chat Completions" : "Responses"}`
      : title;
  }
  developmentOnly(row: Row) {
    const origin = row.connection.config.endpoint;
    return (
      endpointFor(origin, this.endpoints)?.development_only ??
      this.endpoints.some(
        (endpoint) =>
          endpoint.development_only && endpoint.url.startsWith(origin + "/"),
      )
    );
  }
  testText(row: Row) {
    const record = this.host().aiTests.get(row.connection.id);
    if (!record) return "Not tested in this session";
    const result = record.result;
    if (!result.ok) return `Last test failed · ${result.code}`;
    return `${result.model}${result.latency_ms === null ? "" : ` · ${(result.latency_ms / 1000).toFixed(1)} s`}`;
  }
  testTone(row: Row) {
    const record = this.host().aiTests.get(row.connection.id);
    return record ? (record.result.ok ? "success" : "danger") : null;
  }
  openCard(card: ProviderCard) {
    const first = this.visible.find((row) => row.card === card);
    this.host().openAiConnectionDialog({
      card,
      connection: first?.connection ?? null,
    });
  }
  openRow(row: Row, focus?: "model") {
    this.host().openAiConnectionDialog({
      card: row.card,
      connection: row.connection,
      focus,
    });
  }
  menu(row: Row): RowMenuItem[] {
    return [
      { label: "Edit connection", run: () => this.openRow(row) },
      {
        label: "Copy connection ID",
        run: () => void this.copy(row.connection.id),
      },
    ];
  }
  async act(row: ReadinessRow) {
    const fix = row.fix;
    const host = this.host();
    const setup = this.setup;
    if (!fix || !setup || this.running) return;
    if (fix.kind === "navigate") {
      if (fix.route === "operate/workers") await host.navigate("workers");
      else
        this.focus(
          row.id === "weave_ai" ? "#weave-ai-title" : "#ai-providers-title",
        );
      return;
    }
    if (fix.kind !== "operation") return;
    if (fix.operation === "ai_connections.test") {
      const target =
        this.connections.find(
          (connection) => !host.aiTests.get(connection.id)?.result.ok,
        ) ?? this.connections[0];
      if (target)
        host.openAiConnectionDialog({
          card: cardOf(target, this.endpoints),
          connection: target,
          focus: "model",
        });
      return;
    }
    if (fix.operation === "lumi.configuration.write") {
      this.focus("#weave-ai-title");
      return;
    }
    this.running = row.id;
    this.failures = {};
    try {
      if (fix.operation === "ai_setup.publish") {
        await setup.publish();
        this.announcement = "Published the AI actions.";
      } else if (fix.operation === "ai_setup.grant") {
        const targets = this.connections.filter(
          (connection) => !keylessConnection(connection),
        );
        for (const connection of targets) await setup.grant(connection.id);
        this.announcement =
          targets.length === 1
            ? `Authorized ${targets[0].name}.`
            : `Authorized ${targets.length} AI connections.`;
      }
      host.notify(this.announcement);
      await this.recheck();
    } catch (error) {
      const plain = describeError(error);
      if (this.alive)
        this.failures = {
          [row.id]: plain.code
            ? `${plain.message} (${plain.code})`
            : plain.message,
        };
    } finally {
      if (this.alive) {
        this.running = "";
        this.cdr.markForCheck();
      }
    }
  }
  async copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      this.announcement = "Copied.";
    } catch {
      this.announcement = "Copy isn't available here. Select the text instead.";
    }
    this.cdr.markForCheck();
  }
  private focus(selector: string) {
    this.element.nativeElement.querySelector<HTMLElement>(selector)?.focus();
  }
}
