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
// One tile: its card (shape by role, icon, corner badge) and its label
// block. A loop's tile is the header band of its frame, and draws the frame
// around its body too. The canvas places it and handles its clicks; the
// tile only draws.
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  afterRenderEffect,
  computed,
  inject,
  input,
  signal,
  viewChild,
} from "@angular/core";
import { Icon } from "../../icon";
import {
  labelScale,
  labelWidth,
  type Point,
  type TileShape,
} from "./layout-ltr";
import { frameHeaderHeight } from "./loop-frame";
import type { TileBadge, TileBorders } from "./tile-facts";

/** A loop's frame: its tile is the header band at the frame's top. */
export interface FrameView {
  /** The whole frame's height, in canvas units. */
  height: number;
  /** The list as a formula prints it; "…" until it is chosen. */
  items: string;
  /** No list yet: "loop-1 For each item in …" stays whole. */
  pending: boolean;
  /** "· up to 4 at a time", or "". */
  suffix: string;
  /** While a run shows: "7 of 20", "20 of 20" or "failed at item 4", and how far it is (0 to 1). */
  run?: {
    text: string;
    /** "handled": done, and items went on after failures their On error handled. */
    tone: "live" | "done" | "failed" | "stopped" | "handled";
    progress: number | null;
  } | null;
}

export interface TileView {
  id: string;
  /** A workflow step (not the trigger or End): selectable. */
  step: boolean;
  kind: string;
  shape: TileShape;
  /** The same object while the layout stays the same. */
  position: Point;
  width: number;
  height: number;
  icon: string;
  title: string;
  subtitle: string;
  /** The first line of the step's note, shown under its subtitle ("Show on canvas"); "" for none. */
  note: string;
  /** What its data holds, under the label ("3 fields"); "" when it has none. */
  output: string;
  /** On the Runs tab, the line names the run's fact: a failure's code in danger ink, a change in info ink. */
  outputTone?: "danger" | "warning" | "info" | null;
  /** From the card's top to its label block. */
  labelTop: number;
  paintMargin: number;
  name: string;
  tooltip: string;
  badge: TileBadge | null;
  borders: TileBorders;
  pulse: boolean;
  /** A webhook listening for its test event: its border pulses until it stops. */
  listening?: boolean;
  selected: boolean;
  tabIndex: 0 | -1;
  /** Set on a loop's tile. */
  frame?: FrameView | null;
}

@Component({
  selector: "weave-node-tile",
  standalone: true,
  imports: [Icon],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `@let v = view();
    <!-- A loop's header shows its whole sentence in the canvas's own tooltip, on hover and on keyboard focus. -->
    <button
      type="button"
      class="tile-body f-drag-blocker"
      data-action="tile"
      [class.frame-header]="!!v.frame"
      [class.frame-pending]="!!v.frame?.pending"
      [class.frame-tight]="tight()"
      [class.frame-crowded]="crowded()"
      [attr.data-shape]="v.shape"
      [attr.aria-label]="v.name"
      [attr.title]="v.frame ? null : v.tooltip"
      [attr.aria-pressed]="v.step ? v.selected : null"
      [attr.tabindex]="v.tabIndex"
      [style.outline-width.px]="focusWidth()"
      [style.outline-offset.px]="v.frame ? -focusWidth() : focusWidth()"
      [style.--frame-band.px]="v.frame ? frameBand() : null"
      [style.--frame-text-scale]="v.frame ? labelScale() : null"
    >
      <span
        class="tile-shape"
        [class.selected]="v.borders.selected"
        [class.live]="v.borders.live"
        [class.pulse]="v.pulse"
        [class.listening]="!!v.listening"
        [class.error]="v.borders.error"
      ></span>
      @if (v.frame; as frame) {
        <span class="frame-title" #frameTitle>
          <weave-icon class="frame-icon" name="forEach" />
          <span class="frame-name">{{ v.title }}</span>
          <span class="frame-lead">For each item in</span>
          <span class="frame-items">{{ frame.items }}</span>
          <!-- A run's status takes the at-a-time suffix's place and never shortens. -->
          @if (frame.run; as run) {
            <span class="frame-run" [attr.data-tone]="run.tone"
              >·
              @if (run.tone === "live") {
                <span class="frame-live-dot" aria-hidden="true"></span>
              }
              {{ run.text }}</span
            >
          } @else if (frame.suffix) {
            <span class="frame-suffix">{{ frame.suffix }}</span>
          }
        </span>
        @if (frame.run; as run) {
          @if (run.tone === "live" && run.progress !== null) {
            <span
              class="frame-progress"
              [style.--frame-progress]="run.progress"
              aria-hidden="true"
            ></span>
          }
        }
      } @else if (v.kind !== "end") {
        <weave-icon class="tile-icon" [name]="v.icon" />
      }
      @if (!issueBadge() && v.badge; as badge) {
        <span
          class="tile-badge"
          [attr.data-tone]="badge.tone"
          [attr.data-badge]="badge.kind"
        >
          @for (icon of badge.icons; track icon) {
            <weave-icon [name]="icon" [size]="16" />
          }
        </span>
      }
    </button>
    @if (v.frame; as frame) {
      <!-- The frame's border, behind the body's steps: no fill, so the canvas shows through. -->
      <span
        class="loop-frame"
        [attr.data-frame]="v.id"
        [style.height.px]="frame.height"
        [class.selected]="v.borders.selected"
        [class.live]="v.borders.live"
        [class.error]="v.borders.error"
        aria-hidden="true"
      ></span>
    }
    @if (issueBadge(); as badge) {
      <button
        type="button"
        class="tile-issue f-drag-blocker"
        data-action="issues"
        [style.--zoom]="zoom()"
        [attr.tabindex]="v.tabIndex"
        [attr.aria-label]="'Show issues for ' + v.title + ': ' + badge.text"
        [attr.title]="badge.text"
      >
        <span
          class="tile-badge"
          [attr.data-tone]="badge.tone"
          [attr.data-badge]="badge.kind"
          aria-hidden="true"
        >
          @for (icon of badge.icons; track icon) {
            <weave-icon [name]="icon" [size]="16" />
          }
        </span>
      </button>
    }
    @if (v.kind !== "trigger" && !v.frame) {
      <span class="handle handle-in" aria-hidden="true"></span>
    }
    <!-- A loop's name and list are in its header: under the frame, only what its data holds and its note. -->
    @if (v.frame ? v.output || v.note : v.title) {
      <div
        class="tile-label"
        [class.frame-label]="!!v.frame"
        [class.has-note]="!!v.note"
        [class.has-output]="!!v.output"
        [style.top.px]="v.labelTop"
        [style.--label-scale]="labelScale()"
        [style.--label-width.px]="v.shape === 'end' ? null : labelWidth()"
        aria-hidden="true"
      >
        @if (!v.frame) {
          <strong [attr.title]="v.title">{{ v.title }}</strong>
          @if (v.subtitle) {
            <span [attr.title]="v.subtitle">{{ v.subtitle }}</span>
          }
        }
        @if (v.output) {
          <span
            class="tile-output"
            [attr.data-tone]="v.outputTone || null"
            [attr.title]="v.output"
            >{{ v.output }}</span
          >
        }
        @if (v.note) {
          <span class="tile-note" [attr.title]="v.note">{{ v.note }}</span>
        }
      </div>
    }`,
})
export class NodeTile {
  view = input.required<TileView>();
  zoom = input(1);

  issueBadge() {
    const badge = this.view().badge;
    return badge && ["error", "setup", "warning"].includes(badge.kind)
      ? badge
      : null;
  }
  labelScale() {
    return labelScale(this.zoom());
  }
  labelWidth() {
    return labelWidth(this.zoom(), this.view().shape);
  }
  focusWidth() {
    return Math.ceil(2 / this.zoom());
  }
  /** A loop's header band keeps 24 px on screen as the canvas zooms out. */
  readonly frameBand = computed(() => frameHeaderHeight(this.zoom()));
  /**
   * A loop's whole header doesn't fit its band: "For each item in" leaves
   * sight first, since the loop's name and list say what it does, then the
   * suffix and the list shorten.
   */
  readonly tight = signal(false);
  /** Without "For each item in" it still doesn't fit: the suffix leaves too, before the list shortens. */
  readonly crowded = signal(false);
  private readonly frameTitle =
    viewChild<ElementRef<HTMLElement>>("frameTitle");

  constructor() {
    // Measured again after the text or the zoom changes, and when a size
    // changes without them (the header's font arriving).
    afterRenderEffect({
      read: () => {
        this.view();
        this.zoom();
        const title = this.frameTitle()?.nativeElement;
        this.measure(title);
      },
    });
    let observer: ResizeObserver | null = null;
    afterRenderEffect(() => {
      const title = this.frameTitle()?.nativeElement;
      observer?.disconnect();
      observer = null;
      if (!title || typeof ResizeObserver === "undefined") return;
      observer = new ResizeObserver(() => this.measure(title));
      observer.observe(title);
      for (const part of Array.from(title.children)) observer.observe(part);
    });
    inject(DestroyRef).onDestroy(() => observer?.disconnect());
  }
  private measure(title: HTMLElement | undefined) {
    this.tight.set(!!title && headerOverflows(title));
    this.crowded.set(
      !!title &&
        headerOverflows(title, (part) => part.classList.contains("frame-lead")),
    );
  }
}

/**
 * Whether a header's parts at their natural widths (an ellipsis or a hidden
 * part never counts), all but those `without` names, are wider than the
 * room they have. Both
 * are in the header's own units, so the canvas's zoom doesn't matter, and
 * the margins stay the same while a part is hidden: the answer never flips
 * because of itself.
 */
function headerOverflows(
  title: HTMLElement,
  without: (part: Element) => boolean = () => false,
): boolean {
  const parts = Array.from(title.children).filter((part) => !without(part));
  const natural = parts.reduce((sum, part) => {
    const style = getComputedStyle(part);
    const width =
      part instanceof HTMLElement && style.overflow === "hidden"
        ? part.scrollWidth
        : (part as HTMLElement).offsetWidth;
    return (
      sum + width + parseFloat(style.marginLeft) + parseFloat(style.marginRight)
    );
  }, 0);
  return natural > title.clientWidth + 0.5;
}
