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
// The canvas viewport: which part of the workflow graph is on screen and how
// large. Opening a workflow uses a readable fit (75–100 %, top-aligned, the
// spine centered) instead of shrinking a long flow to a few pixels; "Fit
// all" includes the complete graph; zooming keeps the point under the
// pointer in place. Coordinates: world units of the graph, screen pixels of
// the canvas element; screen = world * zoom + pan.

export interface Point {
  x: number;
  y: number;
}
export interface Size {
  width: number;
  height: number;
}
export interface View {
  zoom: number;
  pan: Point;
}
/** The graph's extent in world units. */
export interface Bounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

/** Zoom limits: manual zoom, "Fit all", and the readable fit. */
export const MIN_ZOOM = 0.25;
export const MAX_ZOOM = 2;
export const FIT_ALL_MIN = 0.4;
export const READABLE_MIN = 0.75;
/** Below this zoom, overview mode simplifies labels while keeping insertion. */
export const OVERVIEW_BELOW = 0.6;

const clamp = (value: number, low: number, high: number) =>
  Math.max(low, Math.min(high, value));

/**
 * Opening a workflow and "Tidy layout": zoom to fit the width between 75 %
 * and 100 %, center the main sequence (`spineX`, the Start node's center),
 * and put the top of the graph 32 px from the top. Taller graphs scroll.
 */
export function readableFit(bounds: Bounds, view: Size, spineX: number): View {
  const width = Math.max(1, bounds.maxX - bounds.minX);
  const zoom = clamp(Math.min(1, (view.width - 96) / width), READABLE_MIN, 1);
  // The whole width when it fits, centered; otherwise the spine centered,
  // never pushing the graph's left edge off the canvas.
  const fits = width * zoom <= view.width - 48;
  let x = fits
    ? (view.width - width * zoom) / 2 - bounds.minX * zoom
    : view.width / 2 - spineX * zoom;
  if (!fits && bounds.minX * zoom + x > 24) x = 24 - bounds.minX * zoom;
  return { zoom, pan: { x, y: 32 - bounds.minY * zoom } };
}

/** Fit the complete graph; exceptionally large workflows need a smaller overview. */
export function fitAll(bounds: Bounds, view: Size): View {
  const width = Math.max(1, bounds.maxX - bounds.minX);
  const height = Math.max(1, bounds.maxY - bounds.minY);
  const zoom = Math.min(
    1,
    Math.max(1, view.width - 96) / width,
    Math.max(1, view.height - 128) / height,
  );
  return {
    zoom,
    pan: {
      x: (view.width - width * zoom) / 2 - bounds.minX * zoom,
      y: Math.max(
        32 - bounds.minY * zoom,
        (view.height - height * zoom) / 2 - bounds.minY * zoom,
      ),
    },
  };
}

/** Reveal every lane after a structural edit, within the available canvas. */
export function revealGroup(
  current: View,
  rect: { x: number; y: number; width: number; height: number },
  view: Size,
  margin = 24,
): View {
  const zoom = Math.min(
    current.zoom,
    Math.max(1, view.width - margin * 2) / rect.width,
    Math.max(1, view.height - margin * 2) / rect.height,
  );
  return { zoom, pan: reveal({ ...current, zoom }, rect, view, margin) };
}

/**
 * Zooms by `factor` around a screen point (the pointer, or the canvas
 * center for the buttons): the world point under it stays where it is.
 */
export function zoomAt(
  current: View,
  factor: number,
  at: Point,
  min = MIN_ZOOM,
  max = MAX_ZOOM,
): View {
  const zoom = clamp(current.zoom * factor, min, max);
  const ratio = zoom / current.zoom;
  return {
    zoom,
    pan: {
      x: at.x - (at.x - current.pan.x) * ratio,
      y: at.y - (at.y - current.pan.y) * ratio,
    },
  };
}

/** The wheel's zoom factor: smooth steps instead of jumps between limits. */
export const wheelFactor = (deltaY: number) => Math.exp(-deltaY * 0.0015);

/**
 * The pan that brings a world rectangle inside the view with `margin`
 * screen pixels around it; the current pan when it already is.
 */
export function reveal(
  current: View,
  rect: { x: number; y: number; width: number; height: number },
  view: Size,
  margin = 48,
): Point {
  const left = rect.x * current.zoom + current.pan.x;
  const top = rect.y * current.zoom + current.pan.y;
  const right = left + rect.width * current.zoom;
  const bottom = top + rect.height * current.zoom;
  let { x, y } = current.pan;
  if (right > view.width - margin) x -= right - (view.width - margin);
  if (left + (x - current.pan.x) < margin)
    x += margin - (left + x - current.pan.x);
  if (bottom > view.height - margin) y -= bottom - (view.height - margin);
  if (top + (y - current.pan.y) < margin)
    y += margin - (top + y - current.pan.y);
  return { x, y };
}

/** A stored viewport that is usable: finite numbers and an allowed zoom. */
export function usableViewport(value: unknown): View | null {
  if (!value || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  const zoom = Number(v["zoom"]),
    x = Number(v["x"]),
    y = Number(v["y"]);
  if (![zoom, x, y].every(Number.isFinite)) return null;
  if (zoom <= 0 || zoom > MAX_ZOOM) return null;
  return { zoom, pan: { x, y } };
}
