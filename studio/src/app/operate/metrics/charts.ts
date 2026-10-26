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
// In-house SVG charts for run statistics: stacked columns with lines over
// them, line charts and sparklines. Every series has a legend entry, every
// bucket has tooltip text, and every chart can be read as a table instead.
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  afterNextRender,
  computed,
  inject,
  input,
  signal,
} from "@angular/core";
import {
  chartFrame,
  chartRows,
  columnLayout,
  linePath,
  metricText,
  niceMax,
  seriesStyle,
  seriesName,
  stackColumns,
  ticks,
  timeLabel,
  xLabelIndices,
  type Granularity,
  type StatisticsSeries,
  type Unit,
} from "./chart-model";

const HEIGHT = 160;
const TOP = 8;
const AXIS = 22;
const TICKS = 4;
interface Drawn {
  key: string;
  name: string;
  color: string;
  dashed: boolean;
  path: string;
}

let nextId = 0;

@Component({
  selector: "weave-time-chart",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: "./metrics.css",
  host: { class: "time-chart" },
  template: `
    <div class="chart-head">
      <h3 class="chart-title" [id]="titleId">{{ label() }}</h3>
      <button
        type="button"
        class="tertiary chart-toggle"
        [attr.aria-pressed]="asTable()"
        (click)="asTable.set(!asTable())"
      >
        {{ asTable() ? "Show as chart" : "Show as table" }}
      </button>
    </div>
    @if (asTable()) {
      @let rows = tableRows();
      <div class="chart-table-wrap">
        <table class="chart-table">
          <caption class="sr-only">
            {{
              label()
            }}
          </caption>
          <thead>
            <tr>
              @for (cell of rows[0]; track $index) {
                <th scope="col">{{ cell }}</th>
              }
            </tr>
          </thead>
          <tbody>
            @for (row of rows.slice(1); track $index) {
              <tr>
                @for (cell of row; track $index) {
                  @if ($first) {
                    <th scope="row">{{ cell }}</th>
                  } @else {
                    <td class="numeric">{{ cell }}</td>
                  }
                }
              </tr>
            }
          </tbody>
        </table>
      </div>
    } @else {
      @let g = geometry();
      <svg
        class="chart-frame"
        role="img"
        [attr.aria-labelledby]="titleId"
        [attr.width]="g.width"
        [attr.height]="g.height"
        [attr.viewBox]="'0 0 ' + g.width + ' ' + g.height"
      >
        <g [attr.transform]="'translate(' + g.left + ',' + top + ')'">
          @for (tick of g.yTicks; track $index) {
            <line
              class="chart-grid"
              x1="0"
              [attr.x2]="g.plotWidth"
              [attr.y1]="tick.y"
              [attr.y2]="tick.y"
            />
            <text
              class="chart-tick"
              x="-8"
              [attr.y]="tick.y"
              text-anchor="end"
              dominant-baseline="middle"
            >
              {{ tick.text }}
            </text>
          }
          @for (column of g.columns; track $index) {
            @for (segment of column.segments; track segment.key) {
              <rect
                class="chart-column"
                [attr.x]="column.x"
                [attr.y]="segment.y"
                [attr.width]="g.column"
                [attr.height]="segment.height"
                [attr.fill]="segment.color"
              />
            }
          }
          @for (line of g.lines; track line.key) {
            <path
              class="chart-line"
              [attr.d]="line.path"
              [attr.stroke]="line.color"
              [attr.stroke-dasharray]="line.dashed ? '4 4' : null"
              [attr.transform]="'translate(' + g.band / 2 + ',0)'"
            />
          }
          <line
            class="chart-axis"
            x1="0"
            [attr.x2]="g.plotWidth"
            [attr.y1]="plotHeight"
            [attr.y2]="plotHeight"
          />
          @for (label of g.xLabels; track label.x) {
            <text
              class="chart-tick"
              [attr.x]="label.x"
              [attr.y]="plotHeight + 16"
              text-anchor="middle"
            >
              {{ label.text }}
            </text>
          }
          @for (hover of g.hovers; track $index) {
            <rect
              class="chart-hover"
              [attr.x]="hover.x"
              y="0"
              [attr.width]="g.band"
              [attr.height]="plotHeight"
            >
              <title>{{ hover.title }}</title>
            </rect>
          }
          @if (g.empty) {
            <text
              class="chart-empty"
              [attr.x]="g.plotWidth / 2"
              [attr.y]="plotHeight / 2"
              text-anchor="middle"
              dominant-baseline="middle"
            >
              {{ empty() }}
            </text>
          }
        </g>
      </svg>
      @if (g.legend.length) {
        <ul class="chart-legend" role="list">
          @for (item of g.legend; track item.key) {
            <li class="legend-item">
              <svg
                class="legend-swatch"
                width="16"
                height="10"
                aria-hidden="true"
              >
                @if (item.line) {
                  <line
                    x1="0"
                    x2="16"
                    y1="5"
                    y2="5"
                    [attr.stroke]="item.color"
                    [attr.stroke-dasharray]="item.dashed ? '4 4' : null"
                  />
                } @else {
                  <rect width="10" height="10" [attr.fill]="item.color" />
                }
              </svg>
              {{ item.name }}
            </li>
          }
        </ul>
      }
    }
  `,
})
export class TimeChart {
  series = input<StatisticsSeries[]>([]);
  /** Signals drawn as stacked columns, bottom first. */
  stacked = input<string[]>([]);
  /** Signals drawn as lines over the columns; empty draws every other series as a line. */
  lines = input<string[]>([]);
  unit = input<Unit>("count");
  /** The chart's accessible name and title. */
  label = input.required<string>();
  granularity = input<Granularity>("5m");
  /** What the frame says when no series has a known point. */
  empty = input("No runs in this range");
  readonly asTable = signal(false);
  readonly titleId = `chart-title-${nextId++}`;
  readonly top = TOP;
  readonly plotHeight = HEIGHT;
  private width = signal(600);

  constructor() {
    const element = inject(ElementRef<HTMLElement>)
      .nativeElement as HTMLElement;
    const destroy = inject(DestroyRef);
    afterNextRender(() => {
      const measure = () => {
        const width = Math.floor(element.clientWidth);
        if (width > 0 && width !== this.width()) this.width.set(width);
      };
      measure();
      if (typeof ResizeObserver === "undefined") return;
      const observer = new ResizeObserver(measure);
      observer.observe(element);
      destroy.onDestroy(() => observer.disconnect());
    });
  }

  readonly tableRows = computed(() => {
    const [header, ...rows] = chartRows(this.series());
    const granularity = this.granularity();
    return [
      header,
      ...rows.map(([time, ...cells]) => [
        timeLabel(time, granularity),
        ...cells,
      ]),
    ];
  });

  readonly geometry = computed(() => {
    const series = this.series();
    const stackedSignals = this.stacked();
    const lineSignals = this.lines();
    const unit = this.unit();
    const granularity = this.granularity();
    const stacked = stackedSignals.flatMap((signal) =>
      series.filter((item) => item.signal === signal),
    );
    const lines = series.filter((item) =>
      lineSignals.length
        ? lineSignals.includes(item.signal)
        : !stackedSignals.includes(item.signal),
    );
    const count = Math.max(0, ...series.map((item) => item.points.length));
    const times = series.find((item) => item.points.length === count)?.points;
    const sums = Array.from({ length: count }, (_, index) =>
      stacked.reduce((sum, item) => sum + (item.points[index]?.[1] ?? 0), 0),
    );
    const lineMax = Math.max(
      0,
      ...lines.flatMap((item) => item.points.map(([, value]) => value ?? 0)),
    );
    const max = niceMax(Math.max(lineMax, ...sums));
    const yValues = ticks(max, TICKS);
    const yLabels = yValues.map((value) => metricText(unit, value));
    const frame = chartFrame(this.width(), yLabels);
    const layout = columnLayout(count, frame.plotWidth);
    const keyOf = (item: StatisticsSeries) =>
      item.signal + "|" + Object.values(item.labels).join("|");
    // Categorical colors count only the series that aren't outcomes or percentiles.
    let categorical = 0;
    const styles = new Map(
      series.map((item) => {
        const plain = !/^runs_(succeeded|failed|cancelled)$|^duration_p/.test(
          item.signal,
        );
        return [
          keyOf(item),
          seriesStyle(item.signal, item.labels, plain ? categorical++ : 0),
        ];
      }),
    );
    const stackedKeys = stacked.map(keyOf);
    const segments = stackColumns(
      stacked.map((item) => ({
        key: keyOf(item),
        values: item.points.map(([, value]) => value),
      })),
      { height: HEIGHT, max },
    );
    const drawn: Drawn[] = lines.map((item) => {
      const style = styles.get(keyOf(item))!;
      return {
        key: keyOf(item),
        name: seriesName(item),
        color: style.color,
        dashed: style.dashed,
        path: linePath(
          item.points.map(([, value], index) => [index, value]),
          {
            width: Math.max(0, frame.plotWidth - layout.band),
            height: HEIGHT,
            max,
            count,
          },
        ),
      };
    });
    const longest = Math.max(
      1,
      ...(times ?? []).map(([time]) => timeLabel(time, granularity).length),
    );
    const known = series.some((item) =>
      item.points.some(([, value]) => value !== null),
    );
    return {
      width: this.width(),
      height: TOP + HEIGHT + AXIS,
      left: frame.left,
      plotWidth: frame.plotWidth,
      band: layout.band,
      column: layout.column,
      empty: !known,
      yTicks: yValues.map((value, index) => ({
        y: HEIGHT - (value / max) * HEIGHT,
        text: yLabels[index],
      })),
      columns: segments.map((column, index) => ({
        x: layout.x(index),
        segments: column.map((segment) => ({
          ...segment,
          color: styles.get(segment.key)!.color,
        })),
      })),
      lines: drawn,
      xLabels: times
        ? xLabelIndices(count, frame.plotWidth, longest).map((index) => ({
            x: layout.center(index),
            text: timeLabel(times[index][0], granularity),
          }))
        : [],
      hovers: (times ?? []).map(([time], index) => ({
        x: index * layout.band,
        title: [
          timeLabel(time, granularity),
          ...series.map(
            (item) =>
              `${seriesName(item)}: ${metricText(item.unit, item.points[index]?.[1] ?? null)}`,
          ),
        ].join("\n"),
      })),
      legend: series.map((item) => {
        const style = styles.get(keyOf(item))!;
        return {
          key: keyOf(item),
          name: seriesName(item),
          color: style.color,
          dashed: style.dashed,
          line: !stackedKeys.includes(keyOf(item)),
        };
      }),
    };
  });
}

/** A chart of lines only: rates, durations, queues, backlogs and utilization. */
@Component({
  selector: "weave-line-chart",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: "./metrics.css",
  host: { class: "line-chart" },
  template: `<weave-time-chart
    [series]="series()"
    [unit]="unit()"
    [label]="label()"
    [granularity]="granularity()"
    [empty]="empty()"
  />`,
  imports: [TimeChart],
})
export class LineChart {
  series = input<StatisticsSeries[]>([]);
  unit = input<Unit>("count");
  label = input.required<string>();
  granularity = input<Granularity>("5m");
  empty = input("No runs in this range");
}

/** The last hour's twelve five-minute points under a figure: no axes, no legend. */
@Component({
  selector: "weave-sparkline",
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: "./metrics.css",
  host: { class: "sparkline" },
  template: `
    @let line = path();
    @if (line) {
      <svg
        role="img"
        [attr.aria-label]="description()"
        width="100%"
        height="24"
        viewBox="0 -1 120 24"
        preserveAspectRatio="none"
      >
        <path class="sparkline-line" [attr.d]="line" />
      </svg>
    }
  `,
})
export class Sparkline {
  points = input<Array<number | null>>([]);
  /** The figure's name, as the accessible description says it. */
  name = input.required<string>();
  unit = input<Unit>("count");
  readonly path = computed(() => {
    const points = this.points();
    if (points.filter((value) => value !== null).length < 2) return "";
    const max = Math.max(...points.map((value) => value ?? 0));
    return linePath(
      points.map((value, index) => [index, value]),
      { width: 120, height: 22, max: max > 0 ? max : 1, count: points.length },
    );
  });
  readonly description = computed(() => {
    const known = this.points().filter(
      (value): value is number => value !== null,
    );
    const unit = this.unit();
    return `${this.name()} over the last hour: from ${metricText(unit, known[0] ?? null)} to ${metricText(unit, known.at(-1) ?? null)}`;
  });
}
