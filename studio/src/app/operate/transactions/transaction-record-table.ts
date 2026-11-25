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
// A transaction's record: each step, how it is undone, what happened forward
// and in its undo or confirm, and its operation key. A run's detail lets the
// record's own width pick its columns; the incident drawer always draws one
// row per step, whatever its width.
import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from "@angular/core";
import type { App } from "../../app";
import { copyText } from "../../connection";
import { shortId } from "../../format";
import { Icon } from "../../icon";
import type { RecordRow } from "./transaction-record";

@Component({
  selector: "weave-transaction-record",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon],
  styleUrl: "./transaction-record-table.css",
  template: `<div
    class="txn-record"
    [class.step-rows]="layout() === 'rows'"
    role="table"
    [attr.aria-label]="'Steps of transaction ' + transaction()"
  >
    <div class="txn-step txn-head" role="row">
      <span role="columnheader" class="c-step">Step</span
      ><span role="columnheader" class="c-how">How it's undone</span
      ><span role="columnheader" class="c-forward">Forward</span
      ><span role="columnheader" class="c-undo">Undo or confirm</span
      ><span role="columnheader" class="c-happened" aria-hidden="true"
        >What happened</span
      ><span role="columnheader" class="c-key">Operation key</span>
    </div>
    @for (row of rows(); track row.instanceKey) {
      <div class="txn-step" role="row" [class.stuck]="row.stuck">
        <span role="cell" class="c-step"
          ><strong>{{ row.instanceKey }}</strong>
          @if (row.pending) {
            <span class="step-note">Pending</span>
          }
          @if (row.childRunId; as child) {
            <span class="step-note"
              >Child run
              <button
                type="button"
                class="text-link mono"
                [attr.aria-label]="'Open child run ' + short(child)"
                (click)="openRun.emit(child)"
              >
                {{ short(child) }}
              </button></span
            >
          }</span
        ><span role="cell" class="c-how"
          ><weave-icon [name]="row.howIcon" [size]="16" />{{ row.how }}</span
        ><span role="cell" class="c-forward"
          ><span class="cell-label">Forward</span
          ><span class="step-word">
            @if (row.forward.icon; as icon) {
              <weave-icon
                [name]="icon"
                [size]="16"
                [attr.data-tone]="row.forward.tone"
              />
            }
            {{ row.forward.text }}</span
          >
          @if (!row.undo && row.attempts) {
            <span class="step-note">{{ row.attempts }}</span>
          }</span
        ><span role="cell" class="c-undo" [class.empty]="!row.undo"
          ><span class="cell-label">Undo or confirm</span>
          @if (row.undo; as undo) {
            <span class="step-word">
              @if (undo.icon; as icon) {
                <weave-icon
                  [name]="icon"
                  [size]="16"
                  [attr.data-tone]="undo.tone"
                />
              }
              {{ undo.text }}</span
            >
            @if (row.attempts) {
              <span class="step-note">{{ row.attempts }}</span>
            }
          } @else {
            <span class="none" aria-label="Nothing to undo">—</span>
          }</span
        ><span role="cell" class="c-key"
          ><span class="cell-label">Operation key</span
          ><code>{{ row.operationKey }}</code
          ><button
            type="button"
            class="icon-button copy-key"
            [attr.aria-label]="'Copy operation key ' + row.operationKey"
            (click)="copyKey.emit(row.operationKey)"
          >
            <weave-icon name="copy" [size]="16" /></button
        ></span>
      </div>
    }
  </div>`,
})
export class TransactionRecordTable {
  rows = input.required<RecordRow[]>();
  /** "auto": the record's own width picks 5 columns, 4 or one row per step; "rows": always one row per step. */
  layout = input<"auto" | "rows">("auto");
  /** The transaction's instance key, which names the record. */
  transaction = input.required<string>();
  /** A called step's run to open. */
  openRun = output<string>();
  /** An operation key to copy. */
  copyKey = output<string>();
  readonly short = shortId;
}

/** Copies an operation key and says whether it could. */
export async function copyOperationKey(
  host: Pick<App, "notify">,
  key: string,
): Promise<void> {
  if (await copyText(key)) host.notify("Copied the operation key.");
  else
    host.notify("Studio couldn't copy. Select the operation key and copy it.");
}
