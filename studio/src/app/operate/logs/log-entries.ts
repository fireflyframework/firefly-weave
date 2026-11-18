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
// Log entries as a table: time, level, source, step and message for one run,
// and the workflow and run as well across runs. Text goes through
// interpolation only, so markup in a message shows as text. A withheld run's
// entry, and one Studio can't read, show no text of their own.
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  output,
} from "@angular/core";
import { shortId } from "../../format";
import { Icon } from "../../icon";
import { viewPath } from "../operate-routes";
import type { LogRow, LogRun } from "./logs-api";
import { entryLine, type EntryLine } from "./logs-model";

type Line =
  | ({ kind: "entry"; id: string; run: LogRun | null } & EntryLine)
  | { kind: "withheld" | "unreadable"; id: string };

@Component({
  selector: "weave-log-entries",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon],
  styleUrl: "./logs.css",
  template: `<div
    class="entries"
    role="table"
    aria-label="Log entries"
    [class.with-run]="withRun()"
  >
    <div role="rowgroup">
      <div class="entry head" role="row">
        <span role="columnheader">Time</span
        ><span role="columnheader">Level</span>
        @if (withRun()) {
          <span role="columnheader">Workflow and run</span
          ><span role="columnheader">Step</span
          ><span role="columnheader">Source</span>
        } @else {
          <span role="columnheader">Source</span
          ><span role="columnheader">Step</span>
        }
        <span role="columnheader">Message</span>
      </div>
    </div>
    <div role="rowgroup">
      @for (line of lines(); track line.id) {
        @if (line.kind === "entry") {
          <div class="entry" role="row">
            <span role="cell" class="t"
              ><time
                [attr.datetime]="line.datetime"
                [attr.title]="line.title"
                >{{ line.time }}</time
              ></span
            ><span role="cell" class="lv" [attr.data-tone]="line.tone">{{
              line.level
            }}</span>
            @if (withRun()) {
              <span role="cell" class="wf">
                @if (line.run; as run) {
                  {{ run.workflow }} {{ run.version }} ·
                  <a
                    [href]="runPath(run.id)"
                    [attr.aria-label]="'Open run ' + short(run.id)"
                    (click)="open($event, run.id)"
                    >{{ short(run.id) }}</a
                  >
                  @if (run.test) {
                    <span class="tag">Test</span>
                  }
                }</span
              ><span role="cell" class="step">{{ line.step }}</span
              ><span role="cell" class="src">{{ line.source }}</span>
            } @else {
              <span role="cell" class="src">{{ line.source }}</span
              ><span role="cell" class="step">{{ line.step }}</span>
            }
            <span role="cell" class="msg"
              ><span class="msg-text">{{ line.message }}</span>
              @if (line.flags.length) {
                <span class="meta">
                  @for (flag of line.flags; track flag.text) {
                    <span class="flag"
                      ><weave-icon [name]="flag.icon" [size]="16" />{{
                        flag.text
                      }}</span
                    >
                  }
                </span>
              }
              @if (line.details) {
                <span class="details">{{ line.details }}</span>
              }
            </span>
          </div>
        } @else {
          <div class="entry withheld" role="row">
            <span role="cell" class="t"
              ><span aria-hidden="true">—</span
              ><span class="sr-only">{{
                line.kind === "withheld" ? "Time withheld" : "Time unknown"
              }}</span></span
            ><span role="cell" class="lv">—</span>
            @if (withRun()) {
              <span role="cell" class="wf">—</span>
            }
            <span role="cell" class="step">—</span
            ><span role="cell" class="src">—</span
            ><span role="cell" class="msg">{{
              line.kind === "withheld"
                ? "This run's details are withheld."
                : "Studio can't show this entry."
            }}</span>
          </div>
        }
      }
    </div>
  </div>`,
})
export class LogEntries {
  readonly rows = input.required<readonly LogRow[]>();
  /** Across runs: each entry names its workflow and links to its run. */
  readonly withRun = input(false);
  /** A run link was followed with a plain click. */
  readonly openRun = output<string>();
  readonly short = shortId;
  readonly lines = computed<Line[]>(() =>
    this.rows().map((row) =>
      row.kind === "entry"
        ? { kind: "entry", id: row.id, run: row.run, ...entryLine(row.entry) }
        : { kind: row.kind, id: row.id },
    ),
  );
  runPath(id: string) {
    return viewPath("runs", id);
  }
  /** A plain click opens the run here; a modified one keeps the browser's own. */
  open(event: MouseEvent, id: string) {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey)
      return;
    event.preventDefault();
    this.openRun.emit(id);
  }
}
