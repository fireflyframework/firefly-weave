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
// The Runs tab's list: the workflow's runs newest first, two lines each,
// with Filters, Include test runs, Show older runs and every state the
// list can be in. A listbox with one tab stop: ↑ and ↓ choose a run, Home
// and End the first and last, Enter shows it on the canvas.
import {
  afterNextRender,
  ChangeDetectionStrategy,
  Component,
  computed,
  DestroyRef,
  effect,
  ElementRef,
  inject,
  Injector,
  input,
  signal,
  untracked,
  viewChild,
} from "@angular/core";
import { Modal } from "../../dialog";
import { Icon } from "../../icon";
import { updatedText } from "../../operate/operate-store";
import {
  isUnavailableRun,
  runOrigins,
  runStatuses,
  type RunOrigin,
  type RunStatus,
} from "../../operate/run-contracts";
import { originLabel } from "../../operate/runs/runs-model";
import { statusLabel } from "../../status-labels";
import { runsSheet } from "../state/canvas-commands";
import {
  commandFor,
  eventCombo,
  KEY_PLATFORM,
  type SheetSection,
} from "../state/keymap";
import {
  defaultRunsTabFilters,
  emptyText,
  MORE_THAN_LISTED,
  runRow,
  type RunRow,
  type RunsTabFilters,
} from "./runs-list-model";
import type { App } from "../../app";
import { environmentName, type RunsView } from "./runs-view";

/** A status's icon in a row, beside its label. */
const STATUS_ICONS: Readonly<Record<string, string>> = {
  queued: "schedule",
  running: "spinner",
  waiting: "wait",
  suspended: "wait",
  succeeded: "ok",
  failed: "failCircle",
  cancelled: "off",
  timed_out: "schedule",
};
const SKELETON_ROWS = [0, 1, 2, 3, 4, 5, 6];
/** A version without its build part: "1.3.0+test.d3c2…" reads "1.3.0". */
const shortVersion = (version: string) => version.split("+")[0];
/** Newest version first: 1.10.0 before 1.9.0. */
const byVersion = (a: string, b: string) =>
  b.localeCompare(a, undefined, { numeric: true });

/**
 * The Version menu's choices, newest first: the workflow's published
 * versions, the versions its runs used, and the one chosen. Reads the
 * shell's count of version reads, so versions that load later show.
 */
export function versionChoices(
  app: Pick<App, "workflowVersions" | "workflowVersionsRead">,
  name: string,
  used: readonly string[],
  chosen: string | null,
): string[] {
  app.workflowVersionsRead();
  const seen = new Set<string>();
  for (const version of app.workflowVersions.values())
    if (version.name === name && version.version) seen.add(version.version);
  for (const version of used) seen.add(shortVersion(version));
  if (chosen) seen.add(chosen);
  return [...seen].sort(byVersion);
}

export interface ShownRow extends RunRow {
  icon: string;
  /** A version other than the draft's. */
  older: boolean;
  /** Line 2's text after the icon: the link line or how it started. */
  detail: string;
}
interface Chip {
  label: string;
  remove: () => RunsTabFilters;
}

@Component({
  selector: "weave-runs-list",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Icon, Modal],
  templateUrl: "./runs-list.html",
  styleUrl: "./runs-tab.css",
  host: {
    class: "runs-list",
    "(document:pointerdown)": "outside($event)",
  },
})
export class RunsList {
  readonly tab = input.required<RunsView>();
  readonly statuses = runStatuses.map((value) => ({
    value,
    label: statusLabel("run", value),
  }));
  readonly origins = runOrigins.map((value) => ({
    value,
    label: originLabel(value),
  }));
  readonly skeleton = SKELETON_ROWS;
  readonly moreThanListed = MORE_THAN_LISTED;
  readonly filtersOpen = signal(false);
  readonly sheet = signal<SheetSection[] | null>(null);
  /** Ticks every second: "Updated 3 s ago" and a running run's duration count. */
  readonly now = signal(Date.now());
  /**
   * The time the rows are drawn at: every second while a run in the list is
   * still going (its duration counts), otherwise every 30 seconds for the
   * start times, so a list of finished runs isn't drawn again each second.
   */
  readonly rowsNow = signal(Date.now());
  private readonly listbox = viewChild<ElementRef<HTMLElement>>("listbox");
  private readonly filtersButton =
    viewChild<ElementRef<HTMLElement>>("filtersButton");
  private readonly popover = viewChild<ElementRef<HTMLElement>>("popover");

  readonly state = computed(() => this.tab().state());
  readonly app = computed(() => this.tab().host());
  readonly status = computed(() => {
    const mode = this.tab().mode();
    if (mode !== "ready") return mode;
    return this.state()?.state() ?? "idle";
  });
  readonly filters = computed(
    () => this.state()?.filters() ?? defaultRunsTabFilters(),
  );
  readonly workflow = computed(() => {
    this.tab().state();
    return untracked(() => this.app().model.definition.metadata);
  });
  readonly rows = computed<ShownRow[]>(() => {
    const items = this.state()?.items() ?? [];
    const now = new Date(this.rowsNow());
    const draft = this.workflow().version;
    return items.map((item) => {
      const row = runRow(item, now);
      const version = shortVersion(row.version);
      return {
        ...row,
        version,
        icon: isUnavailableRun(item)
          ? "lock"
          : row.label === "Succeeded with errors"
            ? "warning"
            : (STATUS_ICONS[item.status] ?? "info"),
        older: !row.unavailable && !row.test && version !== draft,
        // A test run's Test tag already says how it started.
        detail: row.unavailable
          ? "Studio can't show this run."
          : (row.link ??
            (row.test && row.origin?.icon === "runHistory"
              ? ""
              : (row.origin?.label ?? ""))),
      };
    });
  });
  readonly chosen = computed(() => this.state()?.chosen()?.id ?? null);
  /** The row with the list's one tab stop. */
  readonly active = computed(() => {
    const rows = this.rows();
    const selected = this.tab().selected();
    return rows.some((row) => row.id === selected) ? selected : rows[0]?.id;
  });
  readonly chips = computed<Chip[]>(() => {
    const filters = this.filters();
    return [
      ...filters.status.map((status) => ({
        label: `Status: ${statusLabel("run", status)}`,
        remove: () => ({
          ...filters,
          status: filters.status.filter((item) => item !== status),
        }),
      })),
      ...(filters.version
        ? [
            {
              label: `Version: ${filters.version}`,
              remove: () => ({ ...filters, version: null }),
            },
          ]
        : []),
      ...(filters.origin
        ? [
            {
              label: `How it started: ${originLabel(filters.origin)}`,
              remove: () => ({ ...filters, origin: null }),
            },
          ]
        : []),
    ];
  });
  /** The workflow's published versions and the versions its runs used. */
  readonly versions = computed(() =>
    versionChoices(
      this.app(),
      this.workflow().name,
      this.state()?.usedVersions() ?? [],
      this.filters().version,
    ),
  );
  readonly empty = computed(() =>
    emptyText(this.workflow().name, this.filters()),
  );
  readonly updated = computed(() =>
    updatedText(this.state()?.updatedAt() ?? null, this.now()),
  );
  /** "just now", "3 s ago": the time alone, where the list is narrower. */
  readonly updatedShort = computed(() =>
    this.updated().replace(/^Updated /, ""),
  );
  readonly environment = computed(() => {
    this.tab().mode();
    return untracked(() => environmentName(this.app()));
  });
  /** The platform's name, as Records says it for an account it doesn't know. */
  readonly platform = computed(() => {
    this.tab().mode();
    return untracked(() => this.app().profile?.name ?? "");
  });

  constructor() {
    const ticker = setInterval(() => {
      const now = Date.now();
      this.now.set(now);
      const going = (this.state()?.items() ?? []).some(
        (item) => !isUnavailableRun(item) && !item.ended_at,
      );
      if (going || now - this.rowsNow() >= 30_000) this.rowsNow.set(now);
    }, 1000);
    inject(DestroyRef).onDestroy(() => clearInterval(ticker));
    // Back to runs on a narrow screen: focus returns to the chosen row.
    // After the list is drawn again: it may have been hidden until now.
    const injector = inject(Injector);
    effect(() => {
      if (!this.tab().focusRow()) return;
      untracked(() => afterNextRender(() => this.focusActive(), { injector }));
    });
  }

  refresh(): void {
    void this.state()?.refresh();
  }
  setFilters(filters: RunsTabFilters): void {
    this.state()?.setFilters(filters);
  }
  toggleStatus(status: RunStatus, on: boolean): void {
    const filters = this.filters();
    this.setFilters({
      ...filters,
      status: on
        ? runStatuses.filter(
            (item) => item === status || filters.status.includes(item),
          )
        : filters.status.filter((item) => item !== status),
    });
  }
  setVersion(value: string): void {
    this.setFilters({ ...this.filters(), version: value || null });
  }
  setOrigin(value: string): void {
    this.setFilters({
      ...this.filters(),
      origin: (value || null) as RunOrigin | null,
    });
  }
  toggleTest(): void {
    const filters = this.filters();
    this.setFilters({ ...filters, includeTest: !filters.includeTest });
  }
  clearFilters(): void {
    this.setFilters(defaultRunsTabFilters());
  }
  removeChip(chip: Chip): void {
    this.setFilters(chip.remove());
    setTimeout(() => this.filtersButton()?.nativeElement.focus());
  }
  more(): void {
    void this.state()?.more();
  }

  openFilters(): void {
    if (this.filtersOpen()) return this.closeFilters();
    this.filtersOpen.set(true);
    setTimeout(() =>
      this.popover()
        ?.nativeElement.querySelector<HTMLElement>("input")
        ?.focus(),
    );
  }
  closeFilters(refocus = true): void {
    this.filtersOpen.set(false);
    if (refocus) setTimeout(() => this.filtersButton()?.nativeElement.focus());
  }
  /** A press outside the popover and its button closes it, focus left where it went. */
  outside(event: Event): void {
    if (!this.filtersOpen()) return;
    const target = event.target as Node | null;
    if (
      target &&
      (this.popover()?.nativeElement.contains(target) ||
        this.filtersButton()?.nativeElement.contains(target))
    )
      return;
    this.closeFilters(false);
  }
  /** Focus left the popover for something outside it (Tab past its end). */
  popoverBlur(event: FocusEvent): void {
    const next = event.relatedTarget as Node | null;
    if (
      !next ||
      this.popover()?.nativeElement.contains(next) ||
      this.filtersButton()?.nativeElement.contains(next)
    )
      return;
    this.closeFilters(false);
  }
  /** The popover keeps focus inside while open; Escape closes it. */
  popoverKey(event: KeyboardEvent): void {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      return this.closeFilters();
    }
    if (event.key !== "Tab") return;
    const stops = [
      ...(this.popover()?.nativeElement.querySelectorAll<HTMLElement>(
        "input, select, button",
      ) ?? []),
    ];
    const first = stops[0];
    const last = stops.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  }

  /** A click on a row: chooses it and shows it on the canvas. */
  choose(row: ShownRow): void {
    this.tab().show(row.id);
  }
  /** The list's keys, from the editor's one shortcut table. */
  listKey(event: KeyboardEvent): void {
    const command = commandFor(eventCombo(event, KEY_PLATFORM), "runs", {
      platform: KEY_PLATFORM,
    });
    const rows = this.rows();
    const index = rows.findIndex((row) => row.id === this.active());
    const move = (to: number) => {
      const row = rows[Math.max(0, Math.min(rows.length - 1, to))];
      if (!row) return;
      this.tab().selected.set(row.id);
      setTimeout(() => this.focusActive());
    };
    switch (command) {
      case "nextRun":
        move(index + 1);
        break;
      case "previousRun":
        move(index - 1);
        break;
      case "firstRun":
        move(0);
        break;
      case "lastRun":
        move(rows.length - 1);
        break;
      case "showRun": {
        const row = rows[index];
        if (row && !row.unavailable) this.tab().show(row.id);
        break;
      }
      case "showShortcuts":
        this.sheet.set(runsSheet(KEY_PLATFORM));
        break;
      default:
        return;
    }
    event.preventDefault();
    event.stopPropagation();
  }
  private focusActive() {
    this.listbox()
      ?.nativeElement.querySelector<HTMLElement>(
        '[role="option"][tabindex="0"]',
      )
      ?.focus();
  }

  // The shell's commands the states offer.
  connect(): void {
    this.app().menuChoice("connect");
  }
  chooseEnvironment(): void {
    this.app().menuChoice("workspace");
  }
  signIn(): void {
    this.app().menuChoice("sign-in");
  }
  /** Checks the platform and who is signed in again, as the banner's Try again does. */
  checkAgain(): void {
    const app = this.app();
    // A sign-in still waiting answers for itself: a check now could replace
    // its banner with "expired".
    if (app.loginWatcher.pending) return;
    const kind = app.platformNotice?.kind;
    if (kind === "offline" || kind === "store") app.noticeAction();
    else void app.verifyPlatform();
  }
  openOperate(): void {
    void this.app().navigate("runs");
  }
}
