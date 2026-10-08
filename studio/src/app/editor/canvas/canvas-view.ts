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
import { FFlowModule } from "@foblex/flow";
import {
  reveal,
  wheelFactor,
  zoomAt,
  type View,
} from "../../designer/viewport";
import { Icon } from "../../icon";
import { RowMenu, type RowMenuItem } from "../../row-menu";
import type { Workflow } from "../../model";
import { loadKindRegistrations } from "../ndv/kinds";
import { ndvRegistry, type KindContext } from "../ndv/registry";
import { moveAllowed, type StepPlace } from "../state/selection";
import type { CanvasHost } from "./canvas-host";
import { CanvasTools } from "./canvas-tools";
import { EdgeLayer, type EdgeView, type InsertView } from "./edge-layer";
import { DROP_REFUSED, dropOutcome } from "./handles";
import {
  LTR,
  edgePath,
  labelScale,
  layoutLtr,
  levelOfDetail,
  midpoint,
  openView,
  type Insertion,
  type LtrLayout,
  type LtrTile,
  type Point,
} from "./layout-ltr";
import { placesOf } from "./navigation";
import { NodeTile, type TileView } from "./node-tile";
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
/** The hover toolbar's size: two 28 px buttons, a 2 px gap, padding and border. */
const TOOLBAR = { width: 64, height: 34 } as const;
/** "Insert a step between a and b" becomes "Move x between a and b". */
const moveName = (name: string, id: string) =>
  name.replace(/^(Insert|Add) a step/, `Move ${id}`);

@Component({
  selector: "weave-canvas-view",
  standalone: true,
  imports: [
    FFlowModule,
    Icon,
    RowMenu,
    NodeTile,
    EdgeLayer,
    SubNodeRow,
    CanvasTools,
  ],
  changeDetection: ChangeDetectionStrategy.Eager,
  encapsulation: ViewEncapsulation.None,
  templateUrl: "./canvas-view.html",
  styleUrl: "./canvas.css",
})
export class CanvasView implements OnInit, DoCheck {
  /** The editor: it owns the workflow and runs every command. */
  host = input.required<CanvasHost>();
  private readonly root = viewChild.required<ElementRef<HTMLElement>>("root");
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
  /** The workflow the view was last fitted to (StructuredCanvasAdapter.opened). */
  private fitted = -1;
  private fitPending = false;
  private revealed = "";
  private readonly positions = new WeakMap<LtrTile, Point>();

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
    const selected = h.model.selected;
    if (selected !== this.revealed) {
      this.revealed = selected;
      if (selected)
        afterNextRender(() => this.revealTile(selected), {
          injector: this.injector,
        });
    }
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
  /** The tile that takes Tab: the selected step, else the first tile. */
  active(): string {
    const layout = this.layout();
    const selected = this.host().model.selected;
    return layout.tiles.some((tile) => tile.id === selected)
      ? selected
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
      selected: string,
      dirty: string,
      active: string,
    ): TileView[] => {
      const ctx = this.host().kindContext();
      return layout.tiles.map((tile) =>
        this.tileView(tile, ctx, facts.get(tile.id) ?? NO_FACTS, {
          selected: tile.id === selected,
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
      h.model.selected,
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
    if (!control) return;
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
  private async openTile(id: string, event: MouseEvent) {
    const h = this.host();
    if (id.startsWith("$trigger:"))
      await h.openWorkflowSection("spec/inputSchema");
    else if (id === "$end") await h.openWorkflowSection("spec/output");
    else await h.selectStep(id, true);
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
    if (tile !== this.hoveredTile) this.hoveredTile = tile;
  }
  unhover() {
    this.hoveredEdge = "";
    this.hoveredTile = "";
  }
  /** Focus on a step, or in its toolbar, shows that step's toolbar. */
  focusIn(event: FocusEvent) {
    this.focusTile = this.toolbarStep(event.target as Element);
  }
  /** Focus left the canvas: only the pointer shows a toolbar now. */
  focusOut(event: FocusEvent) {
    const next = event.relatedTarget;
    if (!(next instanceof Node && this.root().nativeElement.contains(next)))
      this.focusTile = "";
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
    (layout: LtrLayout, focused: string, hovered: string, locked: boolean) =>
      [...new Set([focused, hovered])].flatMap((id) => {
        const tile = id
          ? layout.tiles.find((item) => item.id === id && item.step)
          : undefined;
        return tile
          ? [
              {
                id: tile.id,
                // Its bottom right corner on the tile's top right corner.
                x: tile.x + tile.width - TOOLBAR.width,
                y: tile.y - TOOLBAR.height,
                locked,
                menu: this.menuFor(tile, locked),
              },
            ]
          : [];
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
    );
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
  /** Right-click on a step opens its More menu. */
  contextMenu(event: MouseEvent) {
    const id = (event.target as Element)
      .closest?.("[data-step]")
      ?.getAttribute("data-step");
    if (!id) return;
    event.preventDefault();
    this.hoveredTile = id;
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
  private setView(view: View) {
    this.view = view;
    this.cdr.markForCheck();
  }
}
