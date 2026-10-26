<!--
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
-->

# Read run metrics

Use this page to read how runs behave over time: how many start, how many
fail, how long they take, and how busy the queue, the outbox and the workers
are. It is written for administrators and operators. You need `status.read` in
the environment. Weave keeps these figures itself, in its own database: no
collector or exporter is involved.

## Where to find them

- **In Studio**, open **Operate › Metrics**. Choose a range (1 h, 6 h, 24 h,
  7 d or 30 d) and, optionally, one workflow. The page shows six figures (runs
  started, success rate, failure rate, p50 and p95 run duration, and the oldest
  ready task), six charts (throughput, failure rate, run duration, ready tasks
  by task type, outbox backlog and worker utilization), and a table of
  workflows. Open a workflow to see its steps: executions, failures, p50 and
  p95 duration and p95 queue wait. Every chart can be read as a table with
  **Show as table**, and both tables export to CSV. The range and the workflow
  are part of the page's address, so you can share it. The page refreshes every
  minute while it is visible.
- **On Operate › Overview**, each Load figure has a line for the last hour, and
  **Last 24 h** shows runs per hour, the failure rate and the day's totals.
- **From the CLI**, the three reads are `weave operations statistics series`,
  `workflows` and `steps`:

```bash
weave operations statistics series \
  --from 2026-10-26T00:00:00Z --to 2026-10-27T00:00:00Z \
  --signal runs_started --signal failure_rate
weave operations statistics workflows --from 2026-10-20T00:00:00Z --to 2026-10-27T00:00:00Z
weave operations statistics steps --workflow order-review \
  --from 2026-10-26T00:00:00Z --to 2026-10-27T00:00:00Z
```

The API operations are `run_statistics.series`, `run_statistics.workflows`
and `run_statistics.steps`; see the [API reference](../reference/api.md).

## How runs are counted

- A run counts as **started** in the bucket of its start, and by its
  **outcome** in the bucket of its end: succeeded, failed, canceled or timed
  out.
- **Test runs never count.**
- The **failure rate** is failed and timed-out runs divided by ended runs, a
  fraction from 0 to 1 (Studio shows it as a percent). The success rate is
  succeeded runs divided by ended runs. With no ended run, both are empty, not
  zero. `runs_failed` counts timed-out runs too, so the stacked outcomes add up
  to the ended runs.
- A run whose details the platform withholds from you counts under the
  workflow **(other)**, without a version, so totals stay exact and no withheld
  name appears.
- Step figures count each step by its end. Iterations of a loop and calls of
  the same step count under the step itself. A failure the workflow handled
  still counts as a failure of the step. Queue wait is the time from when the
  step was scheduled to when it started.
- Past 2,000 workflows and versions in one bucket, or past 50 task types, the
  smallest fold into **(other)**. A grouped chart shows the top 4 (up to 20 from
  the API) and folds the rest into **(other)**.

## Percentiles

Durations are kept as histograms of 64 buckets, each √2 times wider than the
one before, from under 1 ms to about 25 days. Histograms add exactly, so a
percentile over a day or a month is computed from the same data as one over
five minutes. p50 and p95 are interpolated inside their bucket, so a reported
percentile is within a factor of √2 of the exact value (for durations from 1 ms
up; under 1 ms the error is under 1 ms). Durations of 2^31 ms or more report
that edge.

## Granularity, retention and freshness

Without `granularity`, a range up to 6 hours is answered in 5-minute buckets,
up to 7 days in hourly buckets, and longer ranges in daily buckets. A series
holds at most 500 points; asking for more (for example 30 days hourly) answers
`422 WV-STATS-RANGE`. An answer holds at most 21 series; more answers
`422 WV-STATS-SERIES`. Contradictory filters, or a range that ends before it
starts or spans more than 400 days, answer `422 WV-FILTER`.

| Granularity | Kept for |
| --- | --- |
| 5 minutes | 2 days |
| 1 hour | 35 days |
| 1 day | 400 days |

Queue, outbox and worker figures are sampled once a minute and kept for 24
hours, and hourly for 35 days.

The operations loop recomputes the last ten minutes of buckets every cycle, so
runs that commit late are counted. Each answer says `complete_until`: buckets
that end at or before that time no longer change. Studio shows it as "Data
complete until 14:32". After the loop was stopped, it catches up one hour per
cycle.

Figures start when the platform is upgraded to a version with run statistics;
runs before that are not counted.

## Metrics and telemetry export

[Telemetry export](observability.md) sends a fixed set of low-cardinality
metrics and traces to an OpenTelemetry collector. Their labels never carry
workflow names, versions, step IDs or task types, and that does not change.
Run metrics answer the per-workflow and per-step questions inside the platform,
under the platform's own access rules.
