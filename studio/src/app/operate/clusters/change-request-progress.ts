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
// A change request's progress: a vertical stepper from checking the target
// to done, the plan's countdown, the workers that must finish first, and the
// actions the viewer may take now. The page performs each action; the server
// checks it again.
import {
  ChangeDetectionStrategy,
  Component,
  EventEmitter,
  Input,
  Output,
} from "@angular/core";
import { toneAttribute } from "../../status-labels";
import type {
  ChangeRequest,
  DrainProgress,
} from "../../operations/deployment-contracts";
import {
  FOUR_EYES_COPY,
  changeActions,
  changeCountdown,
  changeIntentLabel,
  changeStateLabel,
  changeStateTone,
  effectiveState,
  progressSteps,
} from "./change-request-model";

export interface ApplyChoice {
  force: boolean;
  reason: string;
}

@Component({
  selector: "weave-change-request-progress",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.Eager,
  styleUrls: ["./clusters.css", "../operate.css"],
  template: `
    @let state = shown();
    @let actions = allowed();
    <div class="change-summary">
      <div>
        <h3>{{ record.title }}</h3>
        <p class="hint">
          {{ intentLabel(record.intent) }} · {{ targetName }} · requested by
          {{ personName(record.requested_by) }}
        </p>
      </div>
      <span class="status-pill" [attr.data-tone]="tone(state)">{{
        label(state)
      }}</span>
    </div>
    <ol class="progress-steps" aria-label="Progress">
      @for (step of steps(); track step.label) {
        <li
          [attr.data-state]="step.state"
          [attr.aria-current]="step.state === 'current' ? 'step' : null"
        >
          <span class="step-dot" aria-hidden="true"></span>
          <span class="step-text"
            ><strong>{{ step.label }}</strong>
            @if (step.state === "skipped") {
              <small>Not needed for this change</small>
            }
            @if (step.detail) {
              <small>{{ step.detail }}</small>
            }
            @if (step.label === "Plan ready" && step.state === "current") {
              @let time = countdown();
              <small
                class="numeric"
                [attr.data-tone]="countdownTone(time.tone)"
                >{{ deadlineText(time) }}</small
              >
            }
          </span>
        </li>
      }
    </ol>
    @if (record.approval && state !== "awaiting_approval") {
      <p class="hint" role="status">
        Approved by {{ personName(record.approval.principal_id) }}
        @if (state === "approved") {
          — ready to apply
        }
      </p>
    }
    @if (drain && drain.members.length) {
      <section
        class="drain-progress"
        aria-label="Workers finishing their tasks"
      >
        <h4>Workers finishing their tasks</h4>
        <ul>
          @for (member of drain.members; track member.worker_id) {
            <li>
              <code>{{ member.host }}</code
              ><span class="numeric">{{ memberText(member) }}</span>
            </li>
          }
        </ul>
      </section>
    }
    @if (record.warnings.length) {
      <p class="notice" role="status">{{ warningText() }}</p>
    }
    @if (actions.approve === "blocked") {
      <p class="hint four-eyes" id="change-four-eyes">{{ fourEyes }}</p>
    }
    @if (error) {
      <p class="notice" data-tone="danger" role="alert">
        {{ error }}
        @if (code) {
          <small class="support-code">Support code: {{ code }}</small>
        }
      </p>
    }
    @if (actions.apply && actions.reasonToApply && !forcing) {
      <label class="field"
        >Why remove them without letting them finish?
        <textarea
          name="unmatched-reason"
          rows="2"
          maxlength="2000"
          [value]="forceReason"
          (input)="forceReason = text($event)"
        ></textarea>
      </label>
    }
    @if (forcing) {
      <label class="field"
        >Why apply before every worker has finished?
        <textarea
          name="force-reason"
          rows="2"
          maxlength="2000"
          [value]="forceReason"
          (input)="forceReason = text($event)"
        ></textarea>
      </label>
    }
    @if (actions.plan && !planningHere) {
      <p class="hint" id="change-plan-note">
        The target was checked. Whoever makes the plan can't approve it.
      </p>
    }
    <div class="actions progress-actions">
      @if (actions.plan && !planningHere) {
        <button
          type="button"
          class="primary"
          aria-describedby="change-plan-note"
          [disabled]="busy"
          (click)="plan.emit()"
        >
          Plan this change
        </button>
      }
      @if (actions.approve !== "hidden") {
        <button
          type="button"
          class="primary"
          [disabled]="busy || actions.approve === 'blocked'"
          [attr.aria-describedby]="
            actions.approve === 'blocked' ? 'change-four-eyes' : null
          "
          (click)="approve.emit()"
        >
          Approve plan
        </button>
      }
      @if (actions.drain && state === "approved") {
        <button
          type="button"
          [class.primary]="!actions.applyFirst"
          [disabled]="busy"
          (click)="drainNow.emit()"
        >
          Let workers finish
        </button>
      }
      @if (actions.apply) {
        @if (forcing) {
          <button
            type="button"
            class="primary"
            [disabled]="busy || !forceReason.trim()"
            (click)="apply.emit({ force: true, reason: forceReason.trim() })"
          >
            Apply plan now
          </button>
        } @else {
          <button
            type="button"
            [class.primary]="actions.applyFirst"
            [disabled]="busy || (actions.reasonToApply && !forceReason.trim())"
            (click)="
              apply.emit({
                force: false,
                reason: actions.reasonToApply ? forceReason.trim() : '',
              })
            "
          >
            Apply plan
          </button>
          @if (actions.force) {
            <button type="button" [disabled]="busy" (click)="forcing = true">
              Force after timeout
            </button>
          }
        }
      }
      @if (actions.replan) {
        <button type="button" [disabled]="busy" (click)="replan.emit()">
          Re-observe and re-plan
        </button>
      }
      @if (actions.decline) {
        <button
          type="button"
          class="tertiary"
          [disabled]="busy"
          (click)="decline.emit()"
        >
          Decline
        </button>
      }
      @if (actions.cancel) {
        <button
          type="button"
          class="tertiary"
          [disabled]="busy"
          (click)="cancelChange.emit()"
        >
          Cancel change request
        </button>
      }
    </div>
  `,
})
export class ChangeRequestProgress {
  @Input({ required: true }) record!: ChangeRequest;
  @Input() drain: DrainProgress | null = null;
  @Input() targetName = "";
  @Input() viewer: string | null = null;
  @Input() targetStrict = true;
  @Input() busy = false;
  @Input() error = "";
  @Input() code = "";
  @Input() can: (capability: string) => boolean = () => false;
  @Input() personName: (id: string) => string = (id) => id.slice(0, 8);
  /** Server time minus browser time, in milliseconds. */
  @Input() offset = 0;
  @Output() approve = new EventEmitter<void>();
  @Output() decline = new EventEmitter<void>();
  @Output() cancelChange = new EventEmitter<void>();
  @Output() drainNow = new EventEmitter<void>();
  @Output() apply = new EventEmitter<ApplyChoice>();
  @Output() replan = new EventEmitter<void>();
  @Output() plan = new EventEmitter<void>();
  /** True while the page makes the plan itself (the change was just requested in the wizard). */
  @Input() planningHere = false;
  readonly fourEyes = FOUR_EYES_COPY;
  readonly intentLabel = changeIntentLabel;
  forcing = false;
  forceReason = "";
  private now() {
    return Date.now() + this.offset;
  }
  shown() {
    return effectiveState(this.record, this.now());
  }
  allowed() {
    return changeActions(
      this.record,
      this.can,
      this.viewer,
      this.now(),
      this.targetStrict,
    );
  }
  steps() {
    return progressSteps(this.record, this.now());
  }
  countdown() {
    return changeCountdown(this.record, this.now());
  }
  label(state: ChangeRequest["state"]) {
    return changeStateLabel(state);
  }
  tone(state: ChangeRequest["state"]) {
    return toneAttribute(changeStateTone(state));
  }
  /** "Expires in 14:00 · Approve within 6:59" when workers finish first; "Expires in 9:42" otherwise. */
  deadlineText(time: ReturnType<ChangeRequestProgress["countdown"]>) {
    if (time.tone === "expired") return "Expired";
    const expires = this.record.plan_expires_at
      ? Math.max(
          0,
          Math.ceil(
            (Date.parse(this.record.plan_expires_at) - this.now()) / 1000,
          ),
        )
      : 0;
    const clock = `${Math.floor(expires / 60)}:${String(expires % 60).padStart(2, "0")}`;
    return time.note
      ? `Expires in ${clock} · ${time.note}`
      : `Expires in ${clock}`;
  }
  countdownTone(tone: string) {
    return tone === "neutral" || tone === "expired" ? null : tone;
  }
  memberText(member: DrainProgress["members"][number]) {
    if (member.revoked) return "Removed from the platform";
    if (member.presence === "stale") return "Offline";
    if (!member.draining) return "Still taking tasks";
    return member.active_leases
      ? `${member.active_leases} ${member.active_leases === 1 ? "task" : "tasks"} left`
      : "Finished";
  }
  warningText() {
    const texts: Record<string, string> = {
      "WV-DRAIN-UNKNOWN":
        "Some workers could not be matched to a container, so they can't finish their tasks first.",
      "WV-DRAIN-VICTIMS":
        "A worker the plan removes was still running after the change. Check the target.",
    };
    return this.record.warnings.map((code) => texts[code] ?? code).join(" ");
  }
  text(event: Event) {
    return (event.target as HTMLTextAreaElement).value;
  }
}
