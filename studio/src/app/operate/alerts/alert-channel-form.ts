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
// An email channel in Studio's side sheet: its name, the mail connection it
// sends from, project members first and outside addresses behind "Add an
// outside address". Saving makes the person saving the channel's owner, so
// its mail goes out as them; the sheet says so before Save. A refused
// recipient is named under its own field. Recipients stay in this sheet:
// never in the address, storage or the console.
import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  Injector,
  OnInit,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from "@angular/core";
import { ApiError } from "../../api";
import { Modal } from "../../dialog";
import { describeError } from "../../errors";
import { Icon } from "../../icon";
import type {
  AlertChannel,
  AlertChannelDraft,
  AlertRule,
  AlertSettings,
  Recipient,
} from "./alerts-api";
import { recipientProblem } from "./alerts-model";
import type { AlertsStore } from "./alerts-store";

/** A mail connection the person may send from. */
export interface MailConnection {
  revisionId: string;
  name: string;
}
interface Outside {
  id: number;
  value: string;
  problem: string;
}
const NAME_LIMIT = 80;
const RECIPIENT_LIMIT = 10;
const mailbox = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

@Component({
  selector: "weave-alert-channel-form",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon, Modal],
  styleUrls: ["./alerts.css"],
  template: `<weave-modal
    [heading]="heading()"
    placement="side"
    closeLabel="Close"
    (dismiss)="done.emit()"
  >
    <form class="form channel-form" novalidate (submit)="save($event)">
      <div class="field">
        <label for="channel-name">Name</label>
        <input
          id="channel-name"
          autocomplete="off"
          [attr.maxlength]="nameLimit"
          [value]="name()"
          [attr.aria-invalid]="nameProblem() ? 'true' : null"
          [attr.aria-describedby]="
            nameProblem() ? 'channel-name-problem' : null
          "
          (input)="name.set(value($event)); nameProblem.set('')"
        />
        @if (nameProblem()) {
          <p class="problem" id="channel-name-problem">
            <weave-icon name="failCircle" [size]="16" />{{ nameProblem() }}
          </p>
        }
      </div>
      <div class="field">
        <label for="channel-from">Send from</label>
        <select
          id="channel-from"
          [attr.aria-invalid]="fromProblem() ? 'true' : null"
          [attr.aria-describedby]="
            fromProblem()
              ? 'channel-from-problem'
              : ownerHint()
                ? 'channel-from-hint'
                : null
          "
          (change)="from.set(value($event)); fromProblem.set('')"
        >
          @if (!connectionKnown()) {
            <option value="" [selected]="true">
              Choose an email connection
            </option>
          }
          @for (connection of connections(); track connection.revisionId) {
            <option
              [value]="connection.revisionId"
              [selected]="from() === connection.revisionId"
            >
              {{ connection.name }} · your email connection
            </option>
          }
        </select>
        @if (ownerHint()) {
          <p class="hint" id="channel-from-hint">{{ ownerHint() }}</p>
        }
        @if (!connections().length) {
          <p class="warnline">
            <weave-icon name="warning" [size]="16" /><span
              >You have no email connection to send from in {{ environment() }}.
              Add one in Connections first.</span
            >
          </p>
        }
        @if (fromProblem()) {
          <p class="problem" id="channel-from-problem">
            <weave-icon name="failCircle" [size]="16" />{{ fromProblem() }}
          </p>
        }
      </div>
      <div class="field" role="group" aria-labelledby="channel-members-label">
        <span class="field-label" id="channel-members-label"
          >Project members</span
        >
        @if (memberIds().length) {
          <div class="chips">
            @for (id of memberIds(); track id) {
              <span class="chip" [class.warn]="unreachable().has(id)"
                ><weave-icon
                  [name]="unreachable().has(id) ? 'warning' : 'user'"
                  [size]="16" /><span>{{ personName()(id, true) }}</span
                ><button
                  type="button"
                  [attr.aria-label]="'Remove ' + personName()(id, false)"
                  (click)="removeMember(id)"
                >
                  <weave-icon name="close" [size]="16" /></button
              ></span>
            }
          </div>
        }
        @if (memberChoices().length) {
          <select
            aria-label="Add a member"
            (change)="addMember(value($event), $event)"
          >
            <option value="" selected>Add a member</option>
            @for (id of memberChoices(); track id) {
              <option [value]="id">{{ personName()(id, true) }}</option>
            }
          </select>
        }
        @for (id of withoutAddress(); track id) {
          <p class="warnline">
            <weave-icon name="warning" [size]="16" /><span>{{
              id === me()
                ? "You haven't set an alert address. Set one in Settings › Preferences. The rest of the channel still sends."
                : personName()(id, true) +
                  " hasn't set an alert address. Ask them to set one in Settings › Preferences. The rest of the channel still sends."
            }}</span>
          </p>
        }
        @if (memberProblem()) {
          <p class="problem" role="alert">
            <weave-icon name="failCircle" [size]="16" />{{ memberProblem() }}
          </p>
        }
      </div>
      <div class="field" role="group" aria-labelledby="channel-outside-label">
        <span class="field-label" id="channel-outside-label"
          >Outside addresses</span
        >
        @if (!domains().length) {
          <p class="warnline">
            <weave-icon name="warning" [size]="16" /><span
              >Outside addresses can't be used in {{ environment() }} yet. An
              administrator can allow a domain in Settings ›
              Administration.</span
            >
          </p>
        }
        @for (row of outside(); track row.id; let index = $index) {
          <div class="outside">
            <input
              type="email"
              autocomplete="off"
              spellcheck="false"
              [attr.aria-label]="'Outside address ' + (index + 1)"
              [id]="'channel-outside-' + row.id"
              [value]="row.value"
              [attr.aria-invalid]="row.problem ? 'true' : null"
              [attr.aria-describedby]="
                row.problem ? 'channel-outside-problem-' + row.id : null
              "
              (input)="editAddress(row.id, value($event))"
            />
            <button
              type="button"
              class="icon-button"
              [attr.aria-label]="'Remove ' + (row.value || 'this address')"
              (click)="removeAddress(row.id)"
            >
              <weave-icon name="close" [size]="16" />
            </button>
            @if (row.problem) {
              <p class="problem" [id]="'channel-outside-problem-' + row.id">
                <weave-icon name="failCircle" [size]="16" />{{ row.problem }}
              </p>
            }
          </div>
        }
        @if (room()) {
          <button type="button" class="link-button" (click)="addAddress()">
            <weave-icon name="plus" [size]="16" />Add an outside address
          </button>
        }
        <p class="hint">
          Outside addresses need an allowed domain and an opt-in an
          administrator recorded.
        </p>
      </div>
      <p class="says">{{ saying() }}</p>
      @if (saveError()) {
        <p class="problem" role="alert">
          <weave-icon name="failCircle" [size]="16" />{{ saveError() }}
        </p>
      }
      <div class="formacts">
        <button
          type="submit"
          class="primary"
          [attr.aria-disabled]="saving() ? 'true' : null"
        >
          Save channel
        </button>
        @if (channel()) {
          <button
            type="button"
            [attr.aria-disabled]="testing() ? 'true' : null"
            aria-describedby="channel-test-hint"
            (click)="sendTest()"
          >
            Send a test
          </button>
        }
        <span class="spacer"></span>
        <button type="button" class="tertiary" (click)="done.emit()">
          Cancel
        </button>
      </div>
      @if (channel()) {
        <p class="hint" id="channel-test-hint">
          Send a test mails every recipient once at the next delivery, within 10
          seconds. It counts toward the hourly limit.
        </p>
        <p class="hint" role="status">{{ testStatus() }}</p>
      }
    </form>
  </weave-modal>`,
})
export class AlertChannelForm implements OnInit {
  /** The channel to edit, or null to add one. */
  channel = input<AlertChannel | null>(null);
  store = input.required<AlertsStore>();
  rules = input<AlertRule[]>([]);
  settings = input<AlertSettings | null>(null);
  connections = input<MailConnection[]>([]);
  /** People with a role in this project, besides the signed-in person. */
  members = input<string[]>([]);
  me = input.required<string>();
  environment = input("this environment");
  personName = input.required<(id: string, sentenceStart: boolean) => string>();
  done = output<void>();
  private element = inject(ElementRef<HTMLElement>);
  private injector = inject(Injector);
  readonly nameLimit = NAME_LIMIT;
  readonly name = signal("");
  readonly from = signal("");
  readonly memberIds = signal<string[]>([]);
  readonly outside = signal<Outside[]>([]);
  readonly nameProblem = signal("");
  readonly fromProblem = signal("");
  readonly memberProblem = signal("");
  readonly saveError = signal("");
  readonly testStatus = signal("");
  readonly saving = signal(false);
  readonly testing = signal(false);
  private rows = 0;
  private readonly key = crypto.randomUUID();

  constructor() {
    // Saving sends from the person's own connection: when the channel's isn't
    // one they may use (or the list arrives later), their first one is chosen.
    effect(() => {
      const mine = this.connections();
      if (mine.length && !mine.some((item) => item.revisionId === this.from()))
        this.from.set(mine[0].revisionId);
    });
  }

  readonly heading = computed(() => {
    const channel = this.channel();
    return channel ? `Edit ${channel.name}` : "Add a channel";
  });
  readonly domains = computed(() => this.settings()?.recipient_domains ?? []);
  readonly connectionKnown = computed(() =>
    this.connections().some((item) => item.revisionId === this.from()),
  );
  /** Members the platform said it can't reach (saved channel only). */
  readonly unreachable = computed(() => {
    const channel = this.channel();
    const result = new Set<string>();
    if (!channel) return result;
    for (const state of channel.recipient_states) {
      const recipient = channel.recipients[state.index];
      if (recipient && "member" in recipient && state.state !== "ok")
        result.add(recipient.member);
    }
    return result;
  });
  readonly withoutAddress = computed(() => {
    const channel = this.channel();
    if (!channel) return [];
    return channel.recipient_states
      .filter((state) => state.state === "without_address")
      .map((state) => channel.recipients[state.index])
      .filter(
        (recipient): recipient is { member: string } =>
          !!recipient &&
          "member" in recipient &&
          this.memberIds().includes(recipient.member),
      )
      .map((recipient) => recipient.member);
  });
  readonly memberChoices = computed(() => {
    const chosen = new Set(this.memberIds());
    return [this.me(), ...this.members()].filter(
      (id, index, all) => !chosen.has(id) && all.indexOf(id) === index,
    );
  });
  readonly room = computed(
    () => this.memberIds().length + this.outside().length < RECIPIENT_LIMIT,
  );
  /** Who owns the channel now, when it isn't the person editing it. */
  readonly ownerHint = computed(() => {
    const channel = this.channel();
    if (!channel || channel.owner_id === this.me()) return "";
    const owner = this.personName()(channel.owner_id, true);
    return `${owner} owns this channel now; it sends from their email connection.`;
  });
  readonly saying = computed(() => {
    const channel = this.channel();
    const from =
      this.connections().find((item) => item.revisionId === this.from())
        ?.name ?? "the connection you choose";
    if (!channel)
      return `Saving adds the channel to ${this.environment()}. Its mail goes out as you, from ${from}, to the rules that send to it.`;
    const uses = this.rules().filter((rule) =>
      rule.channel_ids.includes(channel.id),
    ).length;
    const approvals = this.settings()?.approval_channel_ids.includes(
      channel.id,
    );
    const users = [
      uses ? `The ${uses} ${uses === 1 ? "rule" : "rules"}` : "",
      approvals ? "the approval notifications" : "",
    ].filter(Boolean);
    const keep = users.length
      ? ` ${users.join(" and ")} that use it keep sending to it.`
      : "";
    return channel.owner_id === this.me()
      ? `Saving keeps its mail going out as you, from ${from}.${keep}`
      : `Saving makes you this channel's owner: its mail goes out as you, from ${from}, instead of as ${this.personName()(channel.owner_id, false)}.${keep}`;
  });

  ngOnInit() {
    const channel = this.channel();
    if (channel) {
      this.name.set(channel.name);
      this.from.set(channel.connection_revision_id);
      this.memberIds.set(
        channel.recipients
          .filter((item): item is { member: string } => "member" in item)
          .map((item) => item.member),
      );
      this.outside.set(
        channel.recipients
          .filter((item): item is { address: string } => "address" in item)
          .map((item) => ({
            id: ++this.rows,
            value: item.address,
            problem: "",
          })),
      );
    }
  }

  value(event: Event) {
    return (event.target as HTMLInputElement | HTMLSelectElement).value;
  }
  addMember(id: string, event: Event) {
    (event.target as HTMLSelectElement).value = "";
    if (!id || !this.room()) return;
    this.memberIds.update((ids) => [...ids, id]);
    this.memberProblem.set("");
  }
  removeMember(id: string) {
    this.memberIds.update((ids) => ids.filter((item) => item !== id));
  }
  addAddress() {
    const id = ++this.rows;
    this.outside.update((rows) => [...rows, { id, value: "", problem: "" }]);
    afterNextRender(
      () =>
        (
          this.element.nativeElement.querySelector(
            `#channel-outside-${id}`,
          ) as HTMLElement | null
        )?.focus(),
      { injector: this.injector },
    );
  }
  editAddress(id: number, value: string) {
    this.outside.update((rows) =>
      rows.map((row) => (row.id === id ? { ...row, value, problem: "" } : row)),
    );
  }
  removeAddress(id: number) {
    this.outside.update((rows) => rows.filter((row) => row.id !== id));
  }

  /** The channel the sheet describes, or null after naming what's missing. */
  private draft(): AlertChannelDraft | null {
    const name = this.name().trim();
    this.nameProblem.set(
      !name
        ? "Name the channel."
        : name.length > NAME_LIMIT
          ? `Keep the name to ${NAME_LIMIT} characters or fewer.`
          : "",
    );
    this.fromProblem.set(
      this.connectionKnown()
        ? ""
        : "Choose the email connection it sends from.",
    );
    this.outside.update((rows) =>
      rows
        .filter((row) => row.value.trim())
        .map((row) => ({
          ...row,
          problem: mailbox.test(row.value.trim())
            ? ""
            : "Enter an email address, such as oncall@example.org.",
        })),
    );
    const recipients: Recipient[] = [
      ...this.memberIds().map((member) => ({ member })),
      ...this.outside().map((row) => ({ address: row.value.trim() })),
    ];
    this.memberProblem.set(
      recipients.length
        ? ""
        : "Add at least one project member or outside address.",
    );
    if (
      this.nameProblem() ||
      this.fromProblem() ||
      this.memberProblem() ||
      this.outside().some((row) => row.problem)
    ) {
      this.focusProblem();
      return null;
    }
    return {
      kind: "email",
      name,
      connection_revision_id: this.from(),
      recipients,
    };
  }
  private focusProblem() {
    afterNextRender(
      () =>
        (
          this.element.nativeElement.querySelector(
            '[aria-invalid="true"]',
          ) as HTMLElement | null
        )?.focus(),
      { injector: this.injector },
    );
  }

  async save(event: Event) {
    event.preventDefault();
    if (this.saving()) return;
    const draft = this.draft();
    if (!draft) return;
    this.saving.set(true);
    this.saveError.set("");
    const channel = this.channel();
    try {
      const saved = await this.store().saveChannel(
        draft,
        channel ? { id: channel.id, revision: channel.revision } : null,
        this.key,
      );
      if (saved) this.done.emit();
    } catch (error) {
      this.refused(error, draft);
    } finally {
      this.saving.set(false);
    }
  }
  /** Names each refused recipient under its own field. */
  private refused(error: unknown, draft: AlertChannelDraft) {
    const plain = describeError(error);
    const result =
      error instanceof ApiError &&
      error.detail &&
      typeof error.detail === "object"
        ? (error.detail as { result?: Record<string, unknown> }).result
        : undefined;
    const refusals = Array.isArray(result?.["recipients"])
      ? (result["recipients"] as { index?: unknown; reason?: unknown }[])
      : [];
    if (plain.code === "WV-ALERT-RECIPIENT" && refusals.length) {
      const members = this.memberIds().length;
      const environment = this.environment();
      for (const refusal of refusals) {
        const index = Number(refusal.index);
        const reason = String(refusal.reason ?? "");
        const recipient = draft.recipients[index];
        if (!recipient) continue;
        if ("member" in recipient)
          this.memberProblem.set(
            `${this.personName()(recipient.member, true)}: ${recipientProblem(reason, "", environment)}`,
          );
        else {
          const row = this.outside()[index - members];
          if (row)
            this.outside.update((rows) =>
              rows.map((item) =>
                item.id === row.id
                  ? {
                      ...item,
                      problem: recipientProblem(
                        reason,
                        recipient.address,
                        environment,
                      ),
                    }
                  : item,
              ),
            );
        }
      }
      this.focusProblem();
      return;
    }
    if (
      plain.code === "WV-ALERT-CHANNEL" &&
      result?.["connection_revision_id"]
    ) {
      this.fromProblem.set(plain.message);
      this.focusProblem();
      return;
    }
    this.saveError.set(
      plain.code ? `${plain.code}: ${plain.message}` : plain.message,
    );
  }

  async sendTest() {
    const channel = this.channel();
    if (!channel || this.testing()) return;
    this.testing.set(true);
    this.testStatus.set("");
    try {
      const answer = await this.store().sendTest(
        channel.id,
        crypto.randomUUID(),
      );
      if (answer)
        this.testStatus.set(
          "Test queued: every recipient gets it at the next delivery, within 10 seconds.",
        );
    } catch (error) {
      this.testStatus.set(
        `Studio couldn't send a test. ${describeError(error).message}`,
      );
    } finally {
      this.testing.set(false);
    }
  }
}
