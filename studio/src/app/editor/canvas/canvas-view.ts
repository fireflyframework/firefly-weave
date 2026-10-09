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
// The left-to-right canvas. One pure layout (layout-ltr.ts) places every
// tile, handle and edge; @foblex/flow pans and zooms it. The editor that
// hosts the canvas owns the workflow and every command: the canvas keeps
// its own view (zoom, pan, what the pointer is on) and asks the host to act.
import {
  ChangeDetectionStrategy,
  ChangeDetectorRef,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  ViewEncapsulation,
  afterNextRender,
  inject,
  input,
  viewChild,
  type DoCheck,
  type OnInit,
} from "@angular/core";
import {
  FControlSchemeController,
  FFlowComponent,
  FFlowModule,
  F_SCROLL_PAN_CONTROL_SCHEME,
  isOnFlowBackground,
  middleButtonEventTrigger,
  primaryButtonEventTrigger,
  provideFFlow,
  withControlScheme,
  type FCanvasChangeEvent,
  type FDragStartedEvent,
  type FSelectionChangeEvent,
  type FTriggerEvent,
} from "@foblex/flow";
import {
  reveal,
  wheelFactor,
  zoomAt,
  type View,
} from "../../designer/viewport";
import { Modal } from "../../dialog";
import { Icon } from "../../icon";
import { RowMenu, type RowMenuItem } from "../../row-menu";
import type { Workflow } from "../../model";
import { loadKindRegistrations } from "../ndv/kinds";
import { ndvRegistry, type KindContext } from "../ndv/registry";
import {
  canvasCommand,
  canvasSheet,
  keyPlatform,
} from "../state/canvas-commands";
import { minimapPinned, setMinimapPinned } from "../state/canvas-preferences";
import { CHOICE_NOT_KEPT } from "../state/editor-flag";
import type { KeymapCommand } from "../state/keymap";
import {
  NO_SELECTION,
  blockedReason,
  covered,
  extended,
  moveAllowed,
  only,
  selectionOf,
  toggledCovering,
  topLevel,
  type Selection,
  type StepPlace,
} from "../state/selection";
import type { CanvasHost } from "./canvas-host";
import { CanvasTools } from "./canvas-tools";
import { EdgeLayer, type EdgeView, type InsertView } from "./edge-layer";
import { DROP_REFUSED, dropOutcome } from "./handles";
import {
  LTR,
  edgePath,
  labelScale,
  labelWidth,
  layoutLtr,
  levelOfDetail,
  midpoint,
  openView,
  pathLabelScale,
  type Insertion,
  type LtrLayout,
  type LtrTile,
  type Point,
} from "./layout-ltr";
import { neighbor, placesOf, type Direction } from "./navigation";
import { NodeTile, type TileView } from "./node-tile";
import { SelectionToolbar } from "./selection-toolbar";
import { SubNodeRow, chipIcon, type SubNodeView } from "./sub-node-row";
import {
  tileBadge,
  tileBorders,
  tileName,
  triggerSubtitle,
  type StepFacts,
  type TileBorders,
} from "./tile-facts";

const NO_FACTS: StepFacts = { errors: [], warnings: [], setup: [] };
const NO_BORDERS: TileBorders = {
  selected: false,
  live: false,
  unapplied: false,
  error: false,
};

/** Recomputes only when an input changed, compared by identity. */
function memo<A extends unknown[], T>(
  compute: (...inputs: A) => T,
): (...inputs: A) => T {
  let last: A | null = null;
  let value!: T;
  return (...inputs: A) => {
    if (!last || inputs.some((item, i) => item !== last![i])) {
      last = inputs;
      value = compute(...inputs);
    }
    return value;
  };
}
/**
 * The hover toolbar: two 28-unit buttons, a 2-unit gap, padding and border.
 * Paths are 224 units apart, so under a step's three-line label block the
 * step below has as little as 9 units of room above it at 40%. The toolbar
 * therefore sits on its step's card, at the top left corner, rising 6 units
 * above it: clear of the corner badge (at most 38 units wide, so from 68
 * units in), of the icon (from 32 units down) and of the label above. A
 * parallel step's bar is 20 units wide with its paths' labels on both
 * sides, so its toolbar sits left of the bar's lower half, under the edge
 * coming in. Nothing assumes one output handle: they are on the right.
 */
const TOOLBAR = { width: 64, height: 34, rise: 6 } as const;
/** "Insert a step between a and b" becomes "Move x between a and b". */
const moveName = (name: string, id: string) =>
  name.replace(/^(Insert|Add) a step/, `Move ${id}`);

@Component({
  selector: "weave-canvas-view",
  standalone: true,
  imports: [
    FFlowModule,
    Icon,
    Modal,
    RowMenu,
    NodeTile,
    EdgeLayer,
    SubNodeRow,
    CanvasTools,
    SelectionToolbar,
  ],
  // A drag on empty canvas draws a selection box; Space, Ctrl or Command
  // with a drag, or the middle button, pans; the canvas handles the wheel.
  providers: [provideFFlow(withControlScheme(F_SCROLL_PAN_CONTROL_SCHEME))],
  changeDetection: ChangeDetectionStrategy.Eager,
  encapsulation: ViewEncapsulation.None,
  host: { "(document:keydown.escape)": "escapeOutside($event)" },
  templateUrl: "./canvas-view.html",
  styleUrl: "./canvas.css",
})
export class CanvasView implements OnInit, DoCheck {
  /** The editor: it owns the workflow and runs every command. */
  host = input.required<CanvasHost>();
  private readonly root = viewChild.required<ElementRef<HTMLElement>>("root");
  /** The flow library keeps a selection of its own, filled only by the selection box. */
  private readonly flow = viewChild(FFlowComponent);
  private readonly cdr = inject(ChangeDetectorRef);
  private readonly injector = inject(Injector);
  /** The step kinds are registered: tiles draw their roles and icons. */
  ready = false;
  /** Zoom and pan: screen = canvas × zoom + pan. */
  view: View = { zoom: 1, pan: { x: 0, y: 0 } };
  /** The edge under the pointer: its "+" shows. */
  hoveredEdge = "";
  /** The step under the pointer, and the one with focus: the hover toolbar's step. */
  hoveredTile = "";
  focusTile = "";
  /** A press on an output handle, a "+" or a step, until it is a click or a drag. */
  private press: {
    kind: "handle" | "tile";
    key: string;
    start: Point;
    pointer: number;
    target: Element;
    dragging: boolean;
  } | null = null;
  /** The edge drawn from a handle while it is dragged, in canvas units. */
  band: { key: string; from: Point; to: Point } | null = null;
  /** The step dragged to a new place, or "". */
  draggingStep = "";
  /** The click that ends a drag does nothing else. */
  private suppressClick = false;
  /** The selected steps and the one with focus. */
  selection: Selection = NO_SELECTION;
  /** The editor's selected step when the canvas last looked. */
  private seenSelected = "";
  /** Space is held: a drag pans. */
  private space = false;
  /** A selection box being drawn, and whether it adds to the selection. */
  private box: { additive: boolean; ids: string[] } | null = null;
  private boxAdditive = false;
  /** Where the last press began, to tell a click from a drag. */
  private downAt: Point | null = null;
  /** "Show minimap" is on. */
  minimapOn = minimapPinned();
  readonly platform = keyPlatform();
  /** The "?" sheet: what works everywhere and on the canvas. */
  readonly sheet = canvasSheet(this.platform);
  sheetOpen = false;
  /** The pointer whose drag Escape ended: letting go of it does nothing. */
  private cancelledPointer: number | null = null;
  /** Browser storage refused "Show minimap" once: said once. */
  private minimapNoticeShown = false;
  /** The view is moving: the minimap shows. */
  panning = false;
  private panningTimer: ReturnType<typeof setTimeout> | undefined;
  /** Steps whose toolbar Escape hid, until the pointer or focus reaches another step. */
  private dismissed: readonly string[] = [];
  /** The workflow the view was last fitted to (StructuredCanvasAdapter.opened). */
  private fitted = -1;
  private fitPending = false;
  private revealed = "";
  private readonly positions = new WeakMap<LtrTile, Point>();

  constructor() {
    inject(DestroyRef).onDestroy(() => clearTimeout(this.panningTimer));
    inject(FControlSchemeController).setScheme({
      nodeMove: () => false,
      createConnection: () => false,
      reassignConnection: () => false,
      nodeResize: () => false,
      nodeRotate: () => false,
      zoom: () => false,
      scrollPan: false,
      canvasMove: (event) =>
        middleButtonEventTrigger(event) ||
        (primaryButtonEventTrigger(event) &&
          (this.space || event.ctrlKey || event.metaKey)),
      selection: (event) => this.boxTrigger(event),
    });
  }
  /** A press on empty canvas, without Space, Ctrl or Command, draws a selection box. */
  readonly boxTrigger = (event: FTriggerEvent): boolean =>
    primaryButtonEventTrigger(event) &&
    !this.space &&
    !event.ctrlKey &&
    !event.metaKey &&
    isOnFlowBackground(event) &&
    !(event.target as Element | null)?.closest?.(
      ".f-minimap, [data-handle], .tile-toolbar",
    );

  ngOnInit() {
    // A lost chunk still draws: unknown kinds draw as app actions, with
    // the labels Studio knows. The failure is logged, not hidden.
    loadKindRegistrations()
      .catch((error: unknown) =>
        console.error("Could not load the step kinds.", error),
      )
      .finally(() => {
        this.ready = true;
        this.cdr.markForCheck();
      });
  }

  ngDoCheck() {
    if (!this.ready) return;
    const h = this.host();
    if (this.fitted !== h.model.opened && !this.fitPending) {
      this.fitPending = true;
      afterNextRender(
        () => {
          this.fitPending = false;
          this.fitIfNew();
        },
        { injector: this.injector },
      );
    }
    this.syncSelection();
    const selected = h.model.selected;
    if (selected !== this.revealed) {
      this.revealed = selected;
      if (selected)
        afterNextRender(() => this.revealTile(selected), {
          injector: this.injector,
        });
    }
  }
  /**
   * The editor's selected step (the one step details show) is the
   * selection's focus: a step selected elsewhere (Outline, an insert, Undo)
   * becomes the selection, and steps that no longer exist leave it.
   */
  private syncSelection() {
    const selected = this.host().model.selected;
    if (selected !== this.seenSelected) {
      this.seenSelected = selected;
      if (!selected) this.selection = NO_SELECTION;
      else if (!this.selection.ids.includes(selected))
        this.selection = only(selected);
      else if (this.selection.focus !== selected)
        this.selection = { ...this.selection, focus: selected };
    }
    const places = this.places();
    if (this.selection.ids.some((id) => !places.has(id)))
      this.selection = selectionOf(
        this.selection.ids.filter((id) => places.has(id)),
        this.selection.focus,
      );
  }

  // ------------------------------------------------------------ layout

  private readonly layoutMemo = memo(
    (_tick: number, workflow: Workflow, _ready: boolean): LtrLayout =>
      layoutLtr(workflow, {
        role: (kind) => ndvRegistry.kind(kind)?.role,
        slots: (kind) =>
          ndvRegistry.subNodes(kind).map((slot) => ({
            id: slot.id,
            label: slot.label,
            required: slot.required,
          })),
        triggers: workflow.spec.steps.length
          ? [{ id: "manual", kind: "manual" }]
          : [],
      }),
  );
  layout(): LtrLayout {
    const h = this.host();
    return this.layoutMemo(h.tick(), h.model.definition, this.ready);
  }
  private readonly placesMemo = memo((layout: LtrLayout) => placesOf(layout));
  places(): Map<string, StepPlace> {
    return this.placesMemo(this.layout());
  }
  /** Nothing can change: a simulation runs, or the source doesn't parse. */
  locked(): boolean {
    const h = this.host();
    return h.editingLocked || h.model.readonly;
  }
  /** The step being placed with "Move to…" or dragged, or "". */
  moving(): string {
    return this.host().connectingNode || this.draggingStep;
  }
  /** Every "+" shows: a step is being placed, or a step kind is dragged in. */
  revealing(): boolean {
    return !!this.moving() || !!this.host().dragPreview;
  }
  level() {
    return levelOfDetail(this.view.zoom);
  }
  /** Text under tiles draws this much larger, so names stay readable. */
  labelScale() {
    return labelScale(this.view.zoom);
  }
  /** A step's label block width, and a parallel step's, in canvas units. */
  labelWidth() {
    return labelWidth(this.view.zoom);
  }
  forkLabelWidth() {
    return labelWidth(this.view.zoom, "fork");
  }
  /** Path labels and "All branches done" draw this much larger, never below 10 px. */
  pathLabelScale() {
    return pathLabelScale(this.view.zoom);
  }
  /** The tile that takes Tab: the selection's focus, else the first tile. */
  active(): string {
    const layout = this.layout();
    const focus = this.selection.focus;
    return focus && layout.tiles.some((tile) => tile.id === focus)
      ? focus
      : (layout.tiles[0]?.id ?? "");
  }
  isFitted(): boolean {
    return this.fitted === this.host().model.opened;
  }

  // ------------------------------------------------------------- tiles

  private readonly tilesMemo = memo(
    (
      layout: LtrLayout,
      facts: ReadonlyMap<string, StepFacts>,
      selection: Selection,
      dirty: string,
      active: string,
    ): TileView[] => {
      const ctx = this.host().kindContext();
      // A selected group shows every step inside it as selected.
      const chosen = covered(selection, this.places());
      return layout.tiles.map((tile) =>
        this.tileView(tile, ctx, facts.get(tile.id) ?? NO_FACTS, {
          selected: chosen.has(tile.id),
          unapplied: tile.id === dirty,
          active: tile.id === active,
        }),
      );
    },
  );
  tiles(): TileView[] {
    const h = this.host();
    return this.tilesMemo(
      this.layout(),
      h.canvasFacts(),
      this.selection,
      h.dirtyStep,
      this.active(),
    );
  }
  private position(tile: LtrTile): Point {
    let point = this.positions.get(tile);
    if (!point) {
      point = { x: tile.x, y: tile.y };
      this.positions.set(tile, point);
    }
    return point;
  }
  private tileView(
    tile: LtrTile,
    ctx: KindContext,
    facts: StepFacts,
    state: { selected: boolean; unapplied: boolean; active: boolean },
  ): TileView {
    const h = this.host();
    const common = {
      id: tile.id,
      kind: tile.kind,
      shape: tile.shape,
      position: this.position(tile),
      width: tile.width,
      height: tile.height,
      labelTop: tile.labelY - tile.y,
      tabIndex: state.active ? (0 as const) : (-1 as const),
      pulse: false,
    };
    if (!tile.step) {
      const trigger = tile.kind === "trigger";
      const subtitle = trigger ? triggerSubtitle(h.model.definition) : "";
      return {
        ...common,
        step: false,
        icon: trigger ? "manualTrigger" : "",
        title: trigger ? "Trigger" : "",
        subtitle,
        name: trigger ? `Trigger, ${subtitle}` : "End, workflow result",
        tooltip: trigger
          ? "Open the workflow inputs"
          : "Open the workflow result",
        badge: null,
        borders: NO_BORDERS,
        selected: false,
      };
    }
    const descriptor = ndvRegistry.kind(tile.kind);
    const kindLabel = descriptor?.label ?? h.label(tile.kind);
    const summary = descriptor?.summary(tile.step, ctx) ?? "";
    const shown = {
      ...facts,
      run: null,
      failure: null,
      stale: null,
      pinned: false,
      unapplied: state.unapplied,
      selected: state.selected,
    };
    const badge = tileBadge(shown);
    return {
      ...common,
      step: true,
      icon: descriptor?.icon ?? "action",
      title: tile.id,
      subtitle: summary || kindLabel,
      name: tileName({
        id: tile.id,
        kindLabel,
        summary,
        status: badge?.text ?? "",
        index: tile.index,
        count: tile.count,
        place: tile.place,
      }),
      tooltip: [`${tile.id}, ${kindLabel}`, badge?.text]
        .filter(Boolean)
        .join(". "),
      badge,
      borders: tileBorders(shown),
      selected: state.selected,
    };
  }

  // -------------------------------------------------- insertion targets

  private readonly targetsMemo = memo(
    (
      layout: LtrLayout,
      places: Map<string, StepPlace>,
      moving: string,
      active: string,
    ) => {
      /** While a step moves: only where it can go, named for the move. */
      const offer = (target: InsertView): InsertView | null => {
        if (!moving) return target;
        if (places.has(moving) && !moveAllowed(places, moving, target.insert))
          return null;
        return { ...target, name: moveName(target.name, moving), moving: true };
      };
      const tab = (tile: string): 0 | -1 => (tile === active ? 0 : -1);
      const pluses = layout.handles.flatMap((handle) => {
        if (!handle.plus) return [];
        const view = offer({
          key: handle.key,
          tile: handle.tile,
          x: handle.plus.x - 12,
          y: handle.plus.y - 12,
          name: handle.name,
          insert: handle.insert,
          moving: false,
          tabIndex: tab(handle.tile),
        });
        return view ? [view] : [];
      });
      const slots = layout.slots.flatMap((slot) => {
        const view = offer({
          key: slot.owner,
          tile: slot.tile,
          x: slot.x,
          y: slot.y,
          name: slot.name,
          insert: slot.insert,
          moving: false,
          tabIndex: tab(slot.tile),
        });
        return view ? [view] : [];
      });
      const onEdges = new Map<string, InsertView | null>();
      for (const edge of layout.edges) {
        if (!edge.insert || !edge.name) continue;
        const mid = midpoint(edge);
        onEdges.set(
          edge.key,
          offer({
            key: edge.key,
            tile: edge.tile,
            x: mid.x - 12,
            y: mid.y - 12,
            name: edge.name,
            insert: edge.insert,
            moving: false,
            tabIndex: tab(edge.tile),
          }),
        );
      }
      return { pluses, slots, onEdges };
    },
  );
  private targets() {
    return this.targetsMemo(
      this.layout(),
      this.places(),
      this.moving(),
      this.active(),
    );
  }
  pluses(): InsertView[] {
    return this.locked() ? [] : this.targets().pluses;
  }
  slots(): InsertView[] {
    return this.targets().slots;
  }
  private readonly edgesMemo = memo(
    (
      layout: LtrLayout,
      onEdges: ReadonlyMap<string, InsertView | null>,
      hovered: string,
      locked: boolean,
    ): EdgeView[] =>
      layout.edges.map((edge) => ({
        edge,
        d: edgePath(edge),
        state: "idle",
        shown: hovered === edge.key,
        plus: locked ? null : (onEdges.get(edge.key) ?? null),
      })),
  );
  edges(): EdgeView[] {
    return this.edgesMemo(
      this.layout(),
      this.targets().onEdges,
      this.hoveredEdge,
      this.locked(),
    );
  }
  private readonly chipsMemo = memo((layout: LtrLayout): SubNodeView[] => {
    if (!layout.subNodes.length) return [];
    const ctx = this.host().kindContext();
    return layout.subNodes.map((row) => {
      const tile = layout.tiles.find((item) => item.id === row.step)!;
      const slots = ndvRegistry.subNodes(tile.kind);
      return {
        step: row.step,
        chips: row.chips.map((chip) => {
          const data = slots
            .find((slot) => slot.id === chip.id)
            ?.chip(tile.step!, ctx) ?? { text: "", state: "ok" as const };
          const warn = data.state === "missing" && chip.required;
          return {
            ...chip,
            icon: warn ? "connections" : chipIcon(chip.id),
            state: data.state,
            warn,
            name: `${chip.label.replace(/\*$/, "")}${chip.required ? ", required" : ""}, ${data.state === "missing" ? "not set" : data.text}`,
          };
        }),
      };
    });
  });
  chips(): SubNodeView[] {
    return this.chipsMemo(this.layout());
  }

  // ----------------------------------------------------------- pointer

  click(event: MouseEvent) {
    if (this.suppressClick) {
      this.suppressClick = false;
      return;
    }
    const control = (event.target as Element).closest<HTMLElement>(
      "[data-action]",
    );
    if (!control) {
      // A click on empty canvas, not the end of a drag, clears the selection.
      const still =
        !!this.downAt &&
        Math.hypot(
          event.clientX - this.downAt.x,
          event.clientY - this.downAt.y,
        ) < 4;
      if (
        still &&
        !(event.target as Element).closest(
          "button, a, [role=menu], [data-handle], [data-tile], .canvas-v2-tools, .selection-toolbar, .tile-toolbar, f-minimap",
        )
      )
        this.clearSelection();
      return;
    }
    const action = control.dataset["action"];
    if (action === "tile") {
      const id = control.closest<HTMLElement>("[data-tile]")?.dataset["tile"];
      if (id) void this.openTile(id, event);
    } else if (action === "insert") this.insertAt(control);
    else if (action === "templates") this.host().showTemplates = true;
    else if (action === "delete") {
      const id = control.dataset["target"];
      if (id && control.getAttribute("aria-disabled") !== "true")
        void this.host().removeSteps([id]);
    }
  }
  /**
   * A click selects the step and shows its details; with Shift, Ctrl or
   * Command it adds the step to the selection, or takes it out.
   */
  private async openTile(id: string, event: MouseEvent) {
    const h = this.host();
    if (id.startsWith("$trigger:"))
      return h.openWorkflowSection("spec/inputSchema");
    if (id === "$end") return h.openWorkflowSection("spec/output");
    if (event.shiftKey || event.ctrlKey || event.metaKey) {
      this.selection = toggledCovering(this.selection, id, this.places());
      this.seenSelected = this.selection.focus ?? "";
      if (this.selection.focus) await h.selectStep(this.selection.focus, false);
      else await h.deselect();
      return;
    }
    this.selection = only(id);
    this.seenSelected = id;
    await h.selectStep(id, true);
  }
  private insertAt(control: HTMLElement) {
    const h = this.host();
    const insert = this.insertOf(control);
    if (!insert || control.getAttribute("aria-disabled") === "true") return;
    const moving = this.moving();
    if (moving) return h.moveStep(moving, insert);
    if (h.isPickerOpenAt(insert)) h.closePicker();
    else
      h.openPicker(
        insert,
        control.getAttribute("aria-label") ?? "Add a step",
        control,
      );
  }
  private insertOf(control: Element): Insertion | null {
    const owner = control.getAttribute("data-insert-owner");
    const index = Number(control.getAttribute("data-insert-index"));
    return owner && Number.isInteger(index) ? { owner, index } : null;
  }
  hover(event: PointerEvent) {
    const target = event.target as Element;
    const edge =
      target.closest?.("[data-edge]")?.getAttribute("data-edge") ?? "";
    if (edge !== this.hoveredEdge) this.hoveredEdge = edge;
    const tile = this.toolbarStep(target);
    if (tile !== this.hoveredTile) {
      this.hoveredTile = tile;
      this.reached(tile);
    }
  }
  unhover() {
    this.hoveredEdge = "";
    this.hoveredTile = "";
  }
  /** Focus on a step, or in its toolbar, shows that step's toolbar. */
  focusIn(event: FocusEvent) {
    this.focusTile = this.toolbarStep(event.target as Element);
    this.reached(this.focusTile);
  }
  /** Focus left the canvas: only the pointer shows a toolbar now, and Space no longer pans. */
  focusOut(event: FocusEvent) {
    const next = event.relatedTarget;
    if (!(next instanceof Node && this.root().nativeElement.contains(next))) {
      this.focusTile = "";
      this.space = false;
    }
  }
  /** The pointer or focus reached a step: a toolbar Escape hid elsewhere may show again. */
  private reached(id: string) {
    if (id && this.dismissed.length && !this.dismissed.includes(id))
      this.dismissed = [];
  }
  /** The step an element belongs to, as a step or in its toolbar; else "". */
  private toolbarStep(element: Element): string {
    const holder = element.closest?.("[data-step], [data-toolbar-for]");
    return (
      holder?.getAttribute("data-step") ??
      holder?.getAttribute("data-toolbar-for") ??
      ""
    );
  }

  // ------------------------------------------------------ hover toolbar

  private readonly toolbarsMemo = memo(
    (
      layout: LtrLayout,
      focused: string,
      hovered: string,
      locked: boolean,
      dismissed: readonly string[],
    ) =>
      [...new Set([focused, hovered])].flatMap((id) => {
        const tile =
          id && !dismissed.includes(id)
            ? layout.tiles.find((item) => item.id === id && item.step)
            : undefined;
        if (!tile) return [];
        const bar = tile.shape === "fork";
        return [
          {
            id: tile.id,
            x: bar ? tile.x + tile.width - TOOLBAR.width : tile.x,
            y: bar
              ? tile.y + tile.height - TOOLBAR.height
              : tile.y - TOOLBAR.rise,
            locked,
            menu: this.menuFor(tile, locked),
          },
        ];
      }),
  );
  /**
   * Delete and More for the step with focus and the one under the pointer.
   * Each keeps its own toolbar, so an open menu never changes step when the
   * pointer moves over another one.
   */
  toolbars() {
    return this.toolbarsMemo(
      this.layout(),
      this.focusTile,
      this.hoveredTile,
      this.locked(),
      this.dismissed,
    );
  }
  /**
   * Hides the toolbars on screen, which appear on hover and focus, until
   * the pointer or focus reaches another step; false when none shows.
   * Focus inside a toolbar returns to its step.
   */
  private hideToolbars(target: Element): boolean {
    const shown = this.toolbars().map((bar) => bar.id);
    if (!shown.length) return false;
    this.dismissed = shown;
    this.cdr.markForCheck();
    const inside = target.closest?.("[data-toolbar-for]");
    if (inside)
      this.root()
        .nativeElement.querySelector<HTMLElement>(
          `[data-tile="${CSS.escape(inside.getAttribute("data-toolbar-for")!)}"] .tile-body`,
        )
        ?.focus();
    return true;
  }
  /**
   * Escape with focus outside the canvas (keys inside it go through key()),
   * for example on the page while the pointer drags: it ends the drag, or
   * else hides the toolbars the pointer shows, and goes no further. A
   * field, a dialog or an open menu takes Escape first; "Move to…" and
   * everything else go on to the editor.
   */
  escapeOutside(event: Event) {
    const h = this.host();
    const target = event.target as Element;
    if (
      event.defaultPrevented ||
      this.root().nativeElement.contains(target) ||
      target.closest?.("input, textarea, select, [role=dialog], .modal-panel")
    )
      return;
    const done = this.dragging()
      ? this.endGesture()
      : !h.connectingNode && !h.dragPreview && this.hideToolbars(target);
    if (done) {
      event.preventDefault();
      event.stopPropagation();
    }
  }
  private menuFor(tile: LtrTile, locked: boolean): RowMenuItem[] {
    const h = this.host();
    const id = tile.id;
    return [
      { label: "Open", run: () => void h.openStep(id, "details") },
      {
        label: "Rename",
        disabled: locked,
        run: () => void h.openStep(id, "rename"),
      },
      {
        label: "Duplicate",
        disabled: locked,
        run: () => void h.duplicateSteps([id]),
      },
      { label: "Move to…", disabled: locked, run: () => h.startMove(id) },
      ...(tile.kind === "humanTask"
        ? [
            {
              label: "Add paths for answers",
              disabled: locked,
              run: () => void h.branchOnDecision(id),
            },
          ]
        : []),
      {
        label: "Delete",
        danger: true,
        disabled: locked,
        run: () => void h.removeSteps([id]),
      },
    ];
  }
  /** Right-click on a step opens its More menu, also after Escape hid its toolbar. */
  contextMenu(event: MouseEvent) {
    const id = (event.target as Element)
      .closest?.("[data-step]")
      ?.getAttribute("data-step");
    if (!id) return;
    event.preventDefault();
    this.hoveredTile = id;
    this.dismissed = this.dismissed.filter((step) => step !== id);
    this.cdr.markForCheck();
    afterNextRender(
      () =>
        this.root()
          .nativeElement.querySelector<HTMLElement>(
            `[data-toolbar-for="${CSS.escape(id)}"] .row-menu-toggle`,
          )
          ?.click(),
      { injector: this.injector },
    );
  }

  // ------------------------------------------------- drags on the canvas

  /** A press on a handle or "+" may draw an edge; on a step, may move it. */
  pointerDown(event: PointerEvent) {
    this.downAt = { x: event.clientX, y: event.clientY };
    this.cancelledPointer = null;
    this.boxAdditive = event.shiftKey;
    if (event.button !== 0 || this.locked()) return;
    const target = event.target as Element;
    const port = target.closest?.("[data-handle]");
    const key = port?.getAttribute("data-handle") ?? "";
    const start = { x: event.clientX, y: event.clientY };
    if (port && this.layout().handles.some((item) => item.key === key)) {
      this.press = {
        kind: "handle",
        key,
        start,
        pointer: event.pointerId,
        target: port,
        dragging: false,
      };
      return;
    }
    const body = target.closest?.(".tile-body");
    const step = body?.closest("[data-step]")?.getAttribute("data-step");
    if (body && step)
      this.press = {
        kind: "tile",
        key: step,
        start,
        pointer: event.pointerId,
        target: body,
        dragging: false,
      };
  }
  pointerMove(event: PointerEvent) {
    const press = this.press;
    if (!press || event.pointerId !== press.pointer) return;
    if (!press.dragging) {
      if (
        Math.hypot(
          event.clientX - press.start.x,
          event.clientY - press.start.y,
        ) < 4
      )
        return;
      press.dragging = true;
      (press.target as HTMLElement).setPointerCapture?.(event.pointerId);
      // A drag replaces a "Move to…" still waiting for its "+".
      if (this.host().connectingNode) this.host().cancelGesture();
    }
    if (press.kind === "handle") {
      const port = this.layout().handles.find((item) => item.key === press.key);
      if (!port) return this.cancelPress();
      this.band = {
        key: press.key,
        from: { x: port.x, y: port.y },
        to: this.toWorld(event.clientX, event.clientY),
      };
    } else this.draggingStep = press.key;
    this.cdr.markForCheck();
  }
  pointerUp(event: PointerEvent) {
    if (event.pointerId === this.cancelledPointer) {
      // Escape ended this drag: letting go drops nothing and clicks nothing.
      this.cancelledPointer = null;
      this.suppressClick = true;
      setTimeout(() => (this.suppressClick = false));
      return;
    }
    const press = this.press;
    if (!press || event.pointerId !== press.pointer) return;
    this.press = null;
    if (!press.dragging) return;
    this.suppressClick = true;
    setTimeout(() => (this.suppressClick = false));
    const h = this.host();
    if (press.kind === "handle") {
      this.band = null;
      const port = this.layout().handles.find((item) => item.key === press.key);
      const box = this.root().nativeElement.getBoundingClientRect();
      const inside =
        event.clientX >= box.left &&
        event.clientX <= box.right &&
        event.clientY >= box.top &&
        event.clientY <= box.bottom;
      // Let go outside the canvas: nothing happens.
      if (port && inside) {
        const point = this.toWorld(event.clientX, event.clientY);
        if (dropOutcome(point, this.layout(), this.view.zoom) === "refused")
          h.notify(DROP_REFUSED);
        else
          h.openPicker(port.insert, port.name, {
            left: event.clientX,
            top: event.clientY,
            width: 0,
            height: 0,
          });
      }
    } else {
      this.draggingStep = "";
      const over = document
        .elementFromPoint(event.clientX, event.clientY)
        ?.closest("[data-insert]");
      const insert = over ? this.insertOf(over) : null;
      const places = this.places();
      if (
        insert &&
        (!places.has(press.key) || moveAllowed(places, press.key, insert))
      )
        h.moveStep(press.key, insert);
      else h.notify("Drop on a highlighted + to move this step.");
    }
    this.cdr.markForCheck();
  }
  /** A handle or a step is being dragged. */
  private dragging(): boolean {
    return !!this.press?.dragging || !!this.band || !!this.draggingStep;
  }
  cancelPress() {
    this.press = null;
    this.band = null;
    this.draggingStep = "";
    this.cdr.markForCheck();
  }
  /** Screen pixels to canvas units. */
  private toWorld(x: number, y: number): Point {
    const box = this.root().nativeElement.getBoundingClientRect();
    return {
      x: (x - box.left - this.view.pan.x) / this.view.zoom,
      y: (y - box.top - this.view.pan.y) / this.view.zoom,
    };
  }

  // ------------------------------------------- step kinds dropped on a "+"

  /** A step kind dragged in from the editor may drop here, while editing is open. */
  dragOver(event: DragEvent) {
    if (
      !this.locked() &&
      event.dataTransfer?.types.includes("application/weave-step")
    )
      this.host().allowDrop(event);
  }
  async dropOn(event: DragEvent) {
    if (!event.dataTransfer?.types.includes("application/weave-step")) return;
    event.preventDefault();
    const over = (event.target as Element).closest?.("[data-insert]");
    const insert = over ? this.insertOf(over) : null;
    if (insert) await this.host().dropStep(event, insert);
    else this.host().notify("Drop the step on a + to place it.");
  }

  // ---------------------------------------------------------- viewport

  wheel(event: WheelEvent) {
    event.preventDefault();
    this.showWhilePanning();
    const box = this.root().nativeElement.getBoundingClientRect();
    if (event.ctrlKey || event.metaKey)
      this.setView(
        zoomAt(this.view, wheelFactor(event.deltaY), {
          x: event.clientX - box.left,
          y: event.clientY - box.top,
        }),
      );
    else {
      const sideways = event.shiftKey && !event.deltaX;
      this.setView({
        zoom: this.view.zoom,
        pan: {
          x: this.view.pan.x - (sideways ? event.deltaY : event.deltaX),
          y: this.view.pan.y - (sideways ? 0 : event.deltaY),
        },
      });
    }
  }
  /** Focus scrolls the canvas to show a tile; that scroll becomes pan. */
  scrolled(event: Event) {
    const element = event.target as HTMLElement;
    const dx = element.scrollLeft;
    const dy = element.scrollTop;
    if (!dx && !dy) return;
    element.scrollLeft = 0;
    element.scrollTop = 0;
    this.setView({
      zoom: this.view.zoom,
      pan: { x: this.view.pan.x - dx, y: this.view.pan.y - dy },
    });
  }
  zoomBy(factor: number) {
    const root = this.root().nativeElement;
    this.setView(
      zoomAt(this.view, factor, {
        x: root.clientWidth / 2,
        y: root.clientHeight / 2,
      }),
    );
  }
  /**
   * Fit view, also the view a workflow opens with: the whole workflow when
   * it fits at 50% or more, else 50% from the trigger (openView).
   */
  fit(): boolean {
    const root = this.root().nativeElement;
    if (!root.clientWidth || !root.clientHeight) return false;
    this.setView(
      openView(this.layout().bounds, {
        width: root.clientWidth,
        height: root.clientHeight,
      }),
    );
    return true;
  }
  /** Opens a workflow the canvas hasn't shown yet with Fit view. */
  fitIfNew() {
    const opened = this.host().model.opened;
    if (this.ready && this.fitted !== opened && this.fit())
      this.fitted = opened;
  }
  /** Pans so a tile and its label sit at least `margin` px inside the canvas. */
  revealTile(id: string, margin = 64) {
    const tile = this.layout().tiles.find((item) => item.id === id);
    const root = this.root().nativeElement;
    if (!tile || !root.clientWidth) return;
    const pan = reveal(
      this.view,
      {
        x: tile.x,
        y: tile.y,
        width: tile.width,
        height: tile.labelY + LTR.labelHeight - tile.y,
      },
      { width: root.clientWidth, height: root.clientHeight },
      margin,
    );
    if (pan.x !== this.view.pan.x || pan.y !== this.view.pan.y)
      this.setView({ zoom: this.view.zoom, pan });
  }

  // ---------------------------------------- selection, box and minimap

  clearSelection() {
    if (!this.selection.ids.length) return;
    this.selection = NO_SELECTION;
    this.seenSelected = "";
    void this.host().deselect();
  }
  /** How many steps the selection toolbar counts: the top-level ones. */
  selectedCount(): number {
    return topLevel(this.selection, this.places()).length;
  }
  duplicateBlocked(): string | null {
    return blockedReason("duplicate", this.selection, this.places());
  }
  /** Why nothing can change now, or null. */
  lockedReason(): string | null {
    const h = this.host();
    if (h.editingLocked) return "Editing is paused while the simulation runs.";
    if (h.model.readonly) return "Fix the source before changing steps.";
    return null;
  }
  async duplicateSelection() {
    const reason = this.lockedReason() ?? this.duplicateBlocked();
    if (reason) return this.host().notify(reason);
    await this.host().duplicateSteps(topLevel(this.selection, this.places()));
  }
  async removeSelection() {
    const reason = this.lockedReason();
    if (reason) return this.host().notify(reason);
    await this.host().removeSteps(topLevel(this.selection, this.places()));
  }
  /** Space held: a drag pans. */
  trackSpace(event: KeyboardEvent) {
    if (event.key === " ") this.space = event.type === "keydown";
  }
  dragStarted(event: FDragStartedEvent) {
    if (event.kind === "selection-area")
      this.box = { additive: this.boxAdditive, ids: [] };
  }
  boxSelected(event: FSelectionChangeEvent) {
    if (this.box) this.box.ids = event.nodeIds;
  }
  /**
   * The boxed steps arrive right after the drag ends: apply them then. A
   * box drawn with Shift adds to the selection. Like a click, it never
   * lists a group together with a step inside it.
   */
  dragEnded() {
    const box = this.box;
    if (!box) return;
    queueMicrotask(() => {
      if (this.box !== box) return;
      this.box = null;
      const places = this.places();
      const ids = box.ids.filter((id) => places.has(id));
      const boxed = box.additive
        ? selectionOf(
            [...this.selection.ids, ...ids],
            ids[0] ?? this.selection.focus,
          )
        : selectionOf(ids);
      const next = selectionOf(topLevel(boxed, places), boxed.focus);
      // The next box starts from nothing: what it adds comes from this
      // selection, not from steps the library still holds.
      this.flow()?.clearSelection();
      this.selection = next;
      this.seenSelected = next.focus ?? "";
      const h = this.host();
      if (next.focus) void h.selectStep(next.focus, false);
      else void h.deselect();
      this.cdr.markForCheck();
    });
  }
  /** The view moved by a drag or the minimap. */
  canvasMoved(event: FCanvasChangeEvent) {
    const { x, y } = event.position;
    if (
      event.scale === this.view.zoom &&
      x === this.view.pan.x &&
      y === this.view.pan.y
    )
      return;
    this.setView({ zoom: event.scale, pan: { x, y } });
    this.showWhilePanning();
  }
  /** "Show minimap": kept for this viewer, or for this session when browser storage refuses. */
  toggleMinimap() {
    this.minimapOn = !this.minimapOn;
    if (!setMinimapPinned(this.minimapOn) && !this.minimapNoticeShown) {
      this.minimapNoticeShown = true;
      this.host().notify(CHOICE_NOT_KEPT);
    }
  }
  /** The minimap shows while the view moves, then fades. */
  private showWhilePanning() {
    this.panning = true;
    clearTimeout(this.panningTimer);
    this.panningTimer = setTimeout(() => {
      this.panning = false;
      this.cdr.markForCheck();
    }, 1200);
  }

  // ----------------------------------------------------------- keyboard

  /**
   * Keys on the canvas: the canvas rows of the shortcut table. Commands
   * about steps act from a step or the canvas itself; on a "+", a tool, a
   * toolbar or a menu, keys act on that control.
   */
  key(event: KeyboardEvent) {
    this.trackSpace(event);
    const target = event.target as HTMLElement;
    // A control that handled the key, or an open menu, owns it.
    if (event.defaultPrevented || target.closest("[role=menu]")) return;
    const typing = target.matches(
      "input, textarea, select, [contenteditable='true']",
    );
    const onStep =
      target === this.root().nativeElement ||
      target.classList.contains("tile-body");
    const command = canvasCommand(event, {
      platform: this.platform,
      typing,
      onStep,
    });
    if (!command || !this.run(command, target)) return;
    event.preventDefault();
    event.stopPropagation();
  }
  /** Runs a command; false when it had nothing to do, so the key goes on. */
  run(command: KeymapCommand, target: HTMLElement): boolean {
    const h = this.host();
    const places = this.places();
    // Focus on a tile acts on it; focus on the canvas, on the selection's focus.
    const focus =
      target.closest("[data-tile]")?.getAttribute("data-tile") ??
      this.selection.focus ??
      "";
    const step = places.has(focus) ? focus : "";
    switch (command) {
      case "zoomIn":
        this.zoomBy(1.2);
        return true;
      case "zoomOut":
        this.zoomBy(1 / 1.2);
        return true;
      case "zoomReset":
        this.zoomBy(1 / this.view.zoom);
        return true;
      case "fitView":
        this.fit();
        return true;
      case "openStepDetails":
        if (focus.startsWith("$trigger:"))
          void h.openWorkflowSection("spec/inputSchema");
        else if (focus === "$end") void h.openWorkflowSection("spec/output");
        else if (step) void h.openStep(step, "details");
        else return false;
        return true;
      case "renameStep":
        if (!step) return false;
        void h.openStep(step, "rename");
        return true;
      case "openAddStep":
      case "searchAddStep":
        this.addStep(target, focus);
        return true;
      case "selectAll":
        return this.selectAll(step);
      case "duplicate": {
        const chosen = this.keyTargets(target, step);
        const reason =
          this.lockedReason() ?? blockedReason("duplicate", chosen, places);
        if (reason) h.notify(reason);
        else void h.duplicateSteps(topLevel(chosen, places));
        return true;
      }
      case "deleteSelection": {
        const ids = topLevel(this.keyTargets(target, step), places);
        if (!ids.length) return false;
        const reason = this.lockedReason();
        if (reason) h.notify(reason);
        else void h.removeSteps(ids);
        return true;
      }
      case "undo":
        h.undo();
        return true;
      case "redo":
        h.redo();
        return true;
      case "previousStep":
        return this.moveFocus(focus, "previous");
      case "nextStep":
        return this.moveFocus(focus, "next");
      case "laneAbove":
        return this.moveFocus(focus, "above");
      case "laneBelow":
        return this.moveFocus(focus, "below");
      case "extendUpstream":
      case "extendDownstream":
        return this.extend(
          step,
          command === "extendUpstream" ? "upstream" : "downstream",
        );
      case "save":
        h.saveShortcut();
        return true;
      case "escape":
        return this.escape(target);
      case "showShortcuts":
        this.sheetOpen = true;
        return true;
      default:
        return false;
    }
  }
  /**
   * The steps a key acts on: the selection when focus is on the canvas or
   * on a step the selection covers, else the step with focus alone. The
   * trigger and End are not steps.
   */
  private keyTargets(target: HTMLElement, step: string): Selection {
    if (!target.closest("[data-tile]")) return this.selection;
    if (!step) return NO_SELECTION;
    return covered(this.selection, this.places()).has(step)
      ? this.selection
      : only(step);
  }
  /** Makes a selection the canvas's, and its focus the editor's selected step. */
  private choose(next: Selection) {
    this.selection = next;
    this.seenSelected = next.focus ?? "";
    const h = this.host();
    if (next.focus) void h.selectStep(next.focus, false);
    else void h.deselect();
  }
  /** Arrow keys: focus (and select) the neighbor in that direction. */
  private moveFocus(from: string, direction: Direction): boolean {
    const next = neighbor(this.layout(), from || this.active(), direction);
    if (!next) return true;
    if (this.places().has(next)) this.choose(only(next));
    this.focusTileLater(next);
    return true;
  }
  /** Shift+← and Shift+→: add the step before or after the focused one, and focus it. */
  private extend(step: string, direction: "upstream" | "downstream"): boolean {
    if (!step) return false;
    const base = this.selection.ids.includes(step)
      ? { ...this.selection, focus: step }
      : only(step);
    const next = extended(base, this.places(), direction);
    this.choose(next);
    this.focusTileLater(next.focus ?? step);
    return true;
  }
  /**
   * Ctrl/Cmd+A: every step of the main sequence, which covers the steps
   * inside its groups. The focused step, or the group holding it, keeps focus.
   */
  private selectAll(step: string): boolean {
    const places = this.places();
    const ids = [...places.values()]
      .filter((place) => place.owner === "root")
      .map((place) => place.id);
    if (!ids.length) return false;
    let top = step;
    while (places.get(top)?.parent) top = places.get(top)!.parent!;
    this.choose(selectionOf(ids, top || ids[0]));
    return true;
  }
  private focusTileLater(id: string) {
    this.cdr.markForCheck();
    afterNextRender(
      () =>
        this.root()
          .nativeElement.querySelector<HTMLElement>(
            `[data-tile="${CSS.escape(id)}"] .tile-body`,
          )
          ?.focus(),
      { injector: this.injector },
    );
  }
  /**
   * N and /: today's step picker for the "+" with focus, after the step
   * with focus, or for the first step.
   */
  private addStep(target: HTMLElement, focus: string) {
    const h = this.host();
    const reason = this.lockedReason();
    if (reason) return h.notify(reason);
    const control = target.closest<HTMLElement>("[data-insert]");
    if (control) return this.insertAt(control);
    const root = this.root().nativeElement;
    if (!h.model.definition.spec.steps.length) {
      h.openPicker(
        { owner: "root", index: 0 },
        "Add first step",
        root.querySelector<HTMLElement>(".empty-trigger") ?? root,
      );
      return;
    }
    const tile = this.layout().tiles.find((item) => item.id === focus);
    const insert = tile?.after ?? {
      owner: "root",
      index: h.model.definition.spec.steps.length,
    };
    const label = !tile?.after
      ? "Add a step at the end"
      : tile.kind === "trigger"
        ? "Add a step at the start"
        : `Add a step after ${tile.id}`;
    const anchor =
      (tile &&
        root.querySelector<HTMLElement>(
          `[data-tile="${CSS.escape(tile.id)}"] .tile-body`,
        )) ||
      root;
    h.openPicker(insert, label, anchor);
  }
  /**
   * Escape on the canvas, one meaning per press: end a drag or "Move to…",
   * else hide the toolbars on screen, else clear the selection. A field, a
   * dialog or an open menu takes Escape first.
   */
  private escape(target: HTMLElement): boolean {
    if (this.endGesture() || this.hideToolbars(target)) return true;
    if (!this.selection.ids.length) return false;
    const focus = this.selection.focus;
    this.clearSelection();
    // The selection toolbar goes with the selection: focus stays on the canvas.
    if (focus && target.closest(".selection-toolbar"))
      this.focusTileLater(focus);
    return true;
  }
  /** Ends a drag, or a step "Move to…" is placing; false when none is under way. */
  private endGesture(): boolean {
    const h = this.host();
    if (this.dragging()) {
      // Letting go of the pointer then drops nothing and clicks nothing.
      if (this.press) this.cancelledPointer = this.press.pointer;
      this.cancelPress();
      return true;
    }
    if (!h.connectingNode && !h.dragPreview) return false;
    const moving = h.connectingNode;
    h.cancelGesture();
    if (moving) {
      h.notify(`Stopped moving ${moving}.`);
      this.focusTileLater(moving);
    }
    return true;
  }
  private setView(view: View) {
    this.view = view;
    this.cdr.markForCheck();
  }
}
