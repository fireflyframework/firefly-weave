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
// A side panel that covers the page (a narrow window, browser zoom) acts as a
// modal side sheet: focus moves into it when it opens, Tab stays inside,
// Escape or a click on the scrim closes it and returns focus to the control
// that opened it, and the content it covers is inert. Where the panel sits
// beside the content it stays an ordinary, non-modal region.
import {
  Directive,
  ElementRef,
  OnDestroy,
  afterEveryRender,
  afterRenderEffect,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
} from "@angular/core";

/** Where each side panel covers the page instead of sitting beside it. */
export const sheetWhen = {
  /**
   * The inspector as a full-height sheet: phones and browser zoom. From 768
   * px it is a column beside the canvas.
   */
  inspector: "(max-width: 767px)",
  /** The step palette whenever it is a popover over the canvas. */
  palette: "(max-width: 1024px)",
  /** A list's detail panel floating over the list. */
  detail: "(max-width: 1024px)",
  /** The expanded simulation panel, where it can't dock beside the canvas. */
  simulation: "(max-width: 767px)",
} as const;

/**
 * What stays usable while a sheet is modal: live regions keep announcing,
 * and dialogs or pickers opened from inside the sheet keep working.
 */
export const keepUsable = [
  "[aria-live]",
  '[role="status"]',
  '[role="alert"]',
  ".modal-backdrop",
  ".toast-region",
  "weave-modal",
  "weave-dialog-host",
  "weave-step-picker",
  "weave-integration-dialogs",
  "weave-start-run-dialog",
  "weave-activation-dialog",
  "weave-resolve-incident-dialog",
  "script",
  "style",
  "link",
].join(", ");

const tabbable =
  'a[href], button, input:not([type="hidden"]), select, textarea, summary, [tabindex]';

/** The tree shape coveredBy() needs; DOM elements have it. */
export interface TreeNode<T> {
  readonly parentElement: T | null;
  readonly children: ArrayLike<T>;
}

/**
 * The elements a modal sheet covers: every sibling of the sheet and of each
 * of its ancestors up to (not including) `root`, except those `keep` accepts.
 * Making them inert leaves only the sheet, and what must stay usable, in the
 * tab order and the accessibility tree.
 */
export function coveredBy<T extends TreeNode<T>>(
  sheet: T,
  root: T,
  keep: (node: T) => boolean,
): T[] {
  const covered: T[] = [];
  for (
    let node = sheet;
    node !== root && node.parentElement;
    node = node.parentElement
  )
    for (const sibling of Array.from(node.parentElement.children))
      if (sibling !== node && !keep(sibling)) covered.push(sibling);
  return covered;
}

/**
 * Where Tab goes in a modal sheet with `count` tabbable controls. `position`
 * is the focused control's index among them; a focused element that is not
 * tabbable (a heading) sits between two indexes (2.5 is after the third
 * control), and NaN means focus is outside the sheet. Returns the index to
 * focus, -1 to keep focus where it is, or null when the browser's own move
 * stays inside the sheet.
 */
export function wrapTab(
  position: number,
  count: number,
  backwards: boolean,
): number | null {
  if (!count) return -1;
  if (Number.isNaN(position)) return backwards ? count - 1 : 0;
  if (backwards) return position <= 0 ? count - 1 : null;
  return position >= count - 1 ? 0 : null;
}

// ---------------------------------------------------------------- the stack

/** Open modal sheets, newest last; only the newest is usable. */
const stack: ModalSheet[] = [];
/** Elements this module made inert (others may have their own reasons). */
const inerted = new Set<Element>();
let scrim: HTMLElement | null = null;
/**
 * The last control focused outside sheets and dialogs: the opener when a
 * sheet opens from a dialog (the simulation setup) that is closing.
 */
let lastOutside: HTMLElement | null = null;
if (typeof document !== "undefined")
  document.addEventListener(
    "focusin",
    (event) => {
      if (
        event.target instanceof HTMLElement &&
        !event.target.closest(".sheet-modal, .modal-panel")
      )
        lastOutside = event.target;
    },
    true,
  );

/** Makes everything the newest sheet covers inert, and nothing else. */
function refresh() {
  const top = stack.at(-1);
  const covered = new Set<Element>(
    top
      ? coveredBy<Element>(
          top.element,
          document.body,
          (node) => node === scrim || node.matches(keepUsable),
        )
      : [],
  );
  for (const element of inerted)
    if (!covered.has(element)) {
      element.removeAttribute("inert");
      inerted.delete(element);
    }
  for (const element of covered)
    if (!inerted.has(element) && !element.hasAttribute("inert")) {
      element.setAttribute("inert", "");
      inerted.add(element);
    }
  if (!top) {
    scrim?.remove();
    scrim = null;
    return;
  }
  if (!scrim) {
    scrim = document.createElement("div");
    scrim.className = "sheet-scrim";
    scrim.setAttribute("aria-hidden", "true");
    // A click on the dimmed page closes the sheet on top.
    scrim.addEventListener("click", () => stack.at(-1)?.dismiss.emit());
    document.body.append(scrim);
  }
}

/**
 * Turns a side panel into a modal side sheet while `weaveModalSheet` is true
 * and the `sheetWhen` media query matches. `sheetDismiss` asks the host to
 * close it (Escape, or a click on the scrim); `sheetReturnFocus` names where
 * focus goes when it closes (the control that opened it otherwise), and
 * `sheetInitialFocus` where focus lands when it opens.
 */
@Directive({
  selector: "[weaveModalSheet]",
  standalone: true,
  host: {
    "(keydown)": "keydown($event)",
    "(pointerdown)": "holdForDrag($event)",
    "(dragend)": "suspended.set(false)",
  },
})
export class ModalSheet implements OnDestroy {
  open = input(false, { alias: "weaveModalSheet" });
  when = input.required<string>({ alias: "sheetWhen" });
  initialFocus = input("", { alias: "sheetInitialFocus" });
  returnFocus = input<(() => HTMLElement | null | undefined) | null>(null, {
    alias: "sheetReturnFocus",
  });
  dismiss = output<void>({ alias: "sheetDismiss" });

  readonly element: HTMLElement = inject(ElementRef).nativeElement;
  readonly suspended = signal(false);
  private readonly matches = signal(false);
  /** True while the panel is open over the page and acts as a modal. */
  readonly modal = computed(
    () => this.open() && this.matches() && !this.suspended(),
  );
  private active = false;
  private opener: HTMLElement | null = null;
  private saved: { role: string | null; tabindex: string | null } | null = null;

  constructor() {
    effect((cleanup) => {
      const query = window.matchMedia(this.when());
      const update = () => this.matches.set(query.matches);
      update();
      query.addEventListener("change", update);
      cleanup(() => query.removeEventListener("change", update));
    });
    afterRenderEffect(() => {
      const modal = this.modal();
      untracked(() => {
        if (modal) this.activate();
        // Closing returns focus; a resize or a drag leaves it where it is.
        else this.deactivate(!this.open());
      });
    });
    // Content rendered while the sheet is open is covered too.
    afterEveryRender(() => {
      if (this.active && stack.at(-1) === this) refresh();
    });
  }

  ngOnDestroy() {
    this.deactivate(true, true);
  }

  /**
   * A press on something draggable (a palette step) releases the page until
   * the press ends or the drag drops, so the step can land on the canvas
   * under the sheet. A click then closes the sheet as usual.
   */
  holdForDrag(event: PointerEvent) {
    const target = event.target as Element | null;
    if (!this.active || !target?.closest?.('[draggable="true"]')) return;
    this.suspended.set(true);
    const release = () => {
      document.removeEventListener("pointerup", release, true);
      this.suspended.set(false);
    };
    // A drag cancels the pointer instead; dragend releases it then.
    document.addEventListener("pointerup", release, true);
  }

  keydown(event: KeyboardEvent) {
    if (!this.active || stack.at(-1) !== this) return;
    if (event.key === "Escape") {
      // A list or menu inside the sheet closes itself first.
      if (event.defaultPrevented) return;
      event.preventDefault();
      event.stopPropagation();
      this.dismiss.emit();
      return;
    }
    if (event.key !== "Tab") return;
    const items = this.tabbables();
    const active = document.activeElement;
    let position = NaN;
    if (active instanceof Element && this.element.contains(active)) {
      position = items.indexOf(active as HTMLElement);
      if (position === -1)
        position =
          items.filter(
            (item) =>
              item.compareDocumentPosition(active) &
              Node.DOCUMENT_POSITION_FOLLOWING,
          ).length - 0.5;
    }
    const next = wrapTab(position, items.length, event.shiftKey);
    if (next === null) return;
    event.preventDefault();
    if (next >= 0) items[next].focus();
  }

  private tabbables() {
    return [...this.element.querySelectorAll<HTMLElement>(tabbable)].filter(
      (item) =>
        item.tabIndex >= 0 &&
        !(item as HTMLButtonElement).disabled &&
        !item.closest("[inert]") &&
        item.getClientRects().length > 0 &&
        getComputedStyle(item).visibility !== "hidden",
    );
  }

  private activate() {
    if (this.active) return refresh();
    this.active = true;
    // The control that opened the sheet; a sheet that opens again from its
    // own controls (the simulation's Expand) keeps the first one.
    const opener = [document.activeElement, lastOutside].find(
      (candidate): candidate is HTMLElement =>
        candidate instanceof HTMLElement &&
        candidate !== document.body &&
        candidate.isConnected &&
        !this.element.contains(candidate) &&
        !candidate.closest(".modal-panel"),
    );
    if (opener) this.opener = opener;
    const host = this.element;
    this.saved = {
      role: host.getAttribute("role"),
      tabindex: host.getAttribute("tabindex"),
    };
    host.setAttribute("role", "dialog");
    host.setAttribute("aria-modal", "true");
    host.classList.add("sheet-modal");
    if (this.saved.tabindex === null) host.setAttribute("tabindex", "-1");
    stack.push(this);
    refresh();
    if (!host.contains(document.activeElement)) this.focusInside();
  }

  private focusInside() {
    const host = this.element;
    const preferred = this.initialFocus()
      ? host.querySelector<HTMLElement>(this.initialFocus())
      : null;
    const target =
      (preferred && preferred.getClientRects().length ? preferred : null) ??
      host.querySelector<HTMLElement>(
        ':is(h1, h2, h3)[tabindex="-1"]:not([hidden])',
      ) ??
      this.tabbables()[0] ??
      host;
    target.focus({ preventScroll: true });
  }

  /** Releases the page; `restore` puts focus back when the sheet closed. */
  private deactivate(restore: boolean, destroying = false) {
    if (!this.active) return;
    this.active = false;
    const index = stack.indexOf(this);
    if (index >= 0) stack.splice(index, 1);
    refresh();
    const host = this.element;
    if (this.saved) {
      if (this.saved.role === null) host.removeAttribute("role");
      else host.setAttribute("role", this.saved.role);
      if (this.saved.tabindex === null) host.removeAttribute("tabindex");
      this.saved = null;
    }
    host.removeAttribute("aria-modal");
    host.classList.remove("sheet-modal");
    // A drag or a resize only pauses the sheet; closing keeps the opener too,
    // for a sheet that opens again from inside (the simulation's Expand).
    if (!restore) return;
    const opener = this.opener;
    const active = document.activeElement;
    const lost =
      !active ||
      active === document.body ||
      !active.isConnected ||
      host.contains(active);
    if (!lost) return;
    const usable = (target: HTMLElement | null | undefined) =>
      !!target &&
      target.isConnected &&
      !target.closest("[inert]") &&
      !(destroying && host.contains(target)) &&
      target.getClientRects().length > 0;
    const preferred = this.returnFocus()?.();
    const target = usable(preferred)
      ? preferred
      : usable(opener)
        ? opener
        : null;
    target?.focus({ preventScroll: true });
  }
}
