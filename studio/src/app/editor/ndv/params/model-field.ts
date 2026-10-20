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
// An AI step's Model field: the approved models of every AI connection the
// person may use, grouped by connection, with what each model can do, and Set
// up AI for a connection that doesn't exist yet. Choosing one writes the
// step's AI settings, its connection slot and its action as one undo step.
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  inject,
  input,
} from "@angular/core";
import { describeError } from "../../../errors";
import { Select, type SelectOption } from "../../../forms/ui/select";
import { Icon } from "../../../icon";
import {
  AiSetupApi,
  type BindableConnection,
} from "../../../integrations/ai-setup-api";
import type { StudioApi } from "../../../api";
import {
  AI_CONNECTOR,
  chooseModel,
  currentModelValue,
  modelOptions,
  parseModelValue,
  profileNameOf,
  profileOf,
  sharingNote,
  type BoundTo,
  type ConnectionModels,
} from "../kinds/ai-task-model";
import type { ParamSpec } from "../registry";
import type { FormSession } from "./form-session";
import { FormWriteError } from "./value-io";

/** The current settings when no listed model matches them. */
const UNLISTED = "\u0000unlisted";
const OFFLINE = "Connect to a platform to choose a model.";
const NONE = "No AI connection yet. Set up AI to add one.";

/** The AI connections and their models, loaded once per account and AI setup change. */
async function loadModels(api: StudioApi): Promise<ConnectionModels[]> {
  const setup = new AiSetupApi(api);
  const connections = (await setup.bindable()).filter(
    (connection: BindableConnection) =>
      connection.enabled && connection.connector === AI_CONNECTOR,
  );
  return Promise.all(
    connections.map(async (connection) => {
      try {
        return {
          connection,
          models: (await setup.models(connection.revision_id)).models,
        };
      } catch (error) {
        return {
          connection,
          models: null,
          error: describeError(error).message,
        };
      }
    }),
  );
}
/**
 * The connection revision last chosen for each set of AI settings, per account:
 * when two connections serve one model, the field keeps showing the one chosen
 * (the field is created again after every change).
 */
const chosenFor = new WeakMap<StudioApi, Map<string, string>>();
const cache = new WeakMap<
  StudioApi,
  { generation: number; groups: Promise<ConnectionModels[]> }
>();

@Component({
  selector: "weave-model-field",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  imports: [Icon, Select],
  template: `<div class="param-model">
    <weave-select
      class="param-select"
      icon="chevronDown"
      [label]="spec().label"
      [hideLabel]="true"
      [controlId]="controlId()"
      [options]="options()"
      [value]="value()"
      [disabled]="!!readOnly() || !session().host.profile"
      [describedBy]="describedBy()"
      placeholder="Choose a model"
      (choose)="choose($event)"
    />
    @if (facts().length) {
      <div
        class="param-model-facts"
        role="group"
        aria-label="What this model can do"
      >
        @for (fact of facts(); track fact) {
          <span class="pill">{{ fact }}</span>
        }
      </div>
    }
    <div class="param-model-footer">
      @if (notice(); as text) {
        <span class="param-model-notice">{{ text }}</span>
      }
      <button
        type="button"
        class="text-link param-open"
        [disabled]="!!readOnly() || !canSetUp()"
        (click)="setUp()"
      >
        <weave-icon name="plus" [size]="16" />Set up AI
      </button>
    </div>
  </div>`,
})
export class ModelField {
  session = input.required<FormSession>();
  spec = input.required<ParamSpec>();
  controlId = input.required<string>();
  describedBy = input("");
  readOnly = input<string | null>(null);
  private readonly lifetime = inject(DestroyRef);
  private groups: ConnectionModels[] | null = null;
  private failure = "";
  private loading: Promise<ConnectionModels[]> | null = null;

  private load(): ConnectionModels[] | null {
    const host = this.session().host;
    if (!host.profile) return null;
    const generation = host.aiSetupGeneration ?? 0;
    let entry = cache.get(host.api);
    if (!entry || entry.generation !== generation) {
      entry = { generation, groups: loadModels(host.api) };
      cache.set(host.api, entry);
    }
    if (entry.groups !== this.loading) {
      const loading = entry.groups;
      this.loading = loading;
      this.groups = null;
      this.failure = "";
      loading.then(
        (groups) => {
          if (this.lifetime.destroyed || this.loading !== loading) return;
          this.groups = groups;
          host.refreshView();
        },
        (error) => {
          if (this.lifetime.destroyed || this.loading !== loading) return;
          cache.delete(host.api);
          this.failure = describeError(error).message;
          host.refreshView();
        },
      );
    }
    return this.groups;
  }
  private boundTo(): BoundTo {
    const session = this.session();
    const ctx = session.controller.ndvContext(session.target);
    return (slot) => ctx.connections.devBinding(slot)?.connectionId ?? null;
  }
  private current(): { label: string; provider: string } | null {
    const { workflow, step } = this.session().subject();
    const profile = step ? profileOf(workflow, step) : null;
    return profile && typeof profile["model"] === "string"
      ? { label: profile["model"], provider: String(profile["provider"]) }
      : null;
  }
  options(): SelectOption[] {
    const groups = this.load() ?? [];
    const options = modelOptions(groups);
    const current = this.current();
    if (current && !this.listedValue())
      options.unshift({
        value: UNLISTED,
        label: current.label,
        description: `${current.provider}. No AI connection you can use serves it here.`,
      });
    return options;
  }
  private listedValue(): string {
    const { workflow, step } = this.session().subject();
    return step
      ? currentModelValue(
          workflow,
          step,
          this.groups ?? [],
          this.boundTo(),
          chosenFor.get(this.session().host.api)?.get(profileNameOf(step)),
        )
      : "";
  }
  value(): string {
    return this.listedValue() || (this.current() ? UNLISTED : "");
  }
  facts(): string[] {
    const chosen = this.options().find((o) => o.value === this.value());
    return chosen && chosen.value !== UNLISTED && chosen.description
      ? chosen.description.split(" · ")
      : [];
  }
  notice(): string {
    if (!this.session().host.profile) return OFFLINE;
    if (this.failure) return this.failure;
    if (this.groups && !this.groups.length) return NONE;
    const { workflow, step } = this.session().subject();
    return step ? sharingNote(workflow, step) : "";
  }
  canSetUp(): boolean {
    const host = this.session().host;
    return !!host.profile && !!host.openAiConnectionDialog;
  }
  setUp(): void {
    if (this.readOnly() || !this.canSetUp()) return;
    this.session().host.openAiConnectionDialog?.({});
  }
  choose(value: string): void {
    const session = this.session();
    if (this.readOnly() || !session.isCurrent()) return;
    const parsed = parseModelValue(value);
    const group = this.groups?.find(
      ({ connection }) => connection.revision_id === parsed?.revisionId,
    );
    const { workflow, step } = session.subject();
    if (!parsed || !group || !step) return;
    try {
      const changes = chooseModel(
        workflow,
        step,
        {
          connectionId: group.connection.connection_id,
          revisionId: group.connection.revision_id,
          connection: group.connection.name,
          provider: group.connection.provider ?? "",
          model: parsed.model,
        },
        this.boundTo(),
      );
      const name = changes.find((change) => change.path[1] === "llmProfiles")
        ?.path[2];
      if (typeof name === "string") {
        const chosen = chosenFor.get(session.host.api) ?? new Map();
        chosen.set(name, group.connection.revision_id);
        chosenFor.set(session.host.api, chosen);
      }
      session.commit(changes, this.spec().id);
    } catch (error) {
      if (error instanceof FormWriteError) session.host.notify(error.message);
      else throw error;
    }
  }
}
