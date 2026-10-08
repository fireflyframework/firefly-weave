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
import type { Workflow } from "../../model";
import { loadKindRegistrations } from "../ndv/kinds";
import { ndvRegistry, type KindContext } from "../ndv/registry";
import { moveAllowed, type StepPlace } from "../state/selection";
import type { CanvasHost } from "./canvas-host";
import { CanvasTools } from "./canvas-tools";
import { EdgeLayer, type EdgeView, type InsertView } from "./edge-layer";
import {
  LTR,
  edgePath,
  fitView,
  layoutLtr,
  levelOfDetail,
  midpoint,
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
/** "Insert a step between a and b" becomes "Move x between a and b". */
const moveName = (name: string, id: string) =>
  name.replace(/^(Insert|Add) a step/, `Move ${id}`);

@Component({
  selector: "weave-canvas-view",
  standalone: true,
  imports: [FFlowModule, Icon, NodeTile, EdgeLayer, SubNodeRow, CanvasTools],
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
  /** The step being placed with "Move to…", or "". */
  moving(): string {
    return this.host().connectingNode;
  }
  /** Every "+" shows: a step is being placed. */
  revealing(): boolean {
    return !!this.moving();
  }
  level() {
    return levelOfDetail(this.view.zoom);
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
    const edge =
      (event.target as Element)
        .closest?.("[data-edge]")
        ?.getAttribute("data-edge") ?? "";
    if (edge !== this.hoveredEdge) this.hoveredEdge = edge;
  }
  unhover() {
    this.hoveredEdge = "";
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
  fit(): boolean {
    const root = this.root().nativeElement;
    if (!root.clientWidth || !root.clientHeight) return false;
    this.setView(
      fitView(this.layout().bounds, {
        width: root.clientWidth,
        height: root.clientHeight,
      }),
    );
    return true;
  }
  /** Fits a workflow the canvas hasn't shown yet. */
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
