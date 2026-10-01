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

# Visual assets

Firefly Weave uses a compact woven W: two diagonal bands with a transparent
underpass. Flat terminals and a consistent band weight keep the symbol readable
at small sizes. Forest and a single muted jade accent connect the identity to
the Firefly family without using PyFly's mark. The banner pairs that symbol with
a single-color wordmark and the descriptor “Workflow orchestration and integration.”

The SVG files are the editable source. They contain native paths, masks, text and
shapes; no scripts, remote fonts, embedded raster images or `foreignObject`.
The artwork is original first-party Firefly Weave work.

| Asset | Intended use | Native size |
| --- | --- | --- |
| [Weave symbol](../assets/weave-logo.svg) | Forest/jade mark on white or light neutral surfaces | 256 × 256 |
| [Monochrome symbol](../assets/weave-logo-mono.svg) | One-color reproduction on light surfaces | 256 × 256 |
| [Reversed symbol](../assets/weave-logo-reversed.svg) | White mark on dark surfaces | 256 × 256 |
| [Banner](../assets/banner.svg) | README and project overview; opaque white background | 1120 × 280 |
| [System context](diagrams/system-context.svg) | Component and trust-boundary overview | 1120 × 840 |
| [Compiler and execution](diagrams/compiler-execution.svg) | Static checks and runtime lifecycle | 1120 × 880 |

## Palette and typography

Use forest `#173D34` for the wordmark and primary band, jade `#367D68` for the
single accent band, slate `#62706A` for secondary text, white `#FFFFFF` for the
banner, and mist `#EEF4F0` as an optional surrounding neutral. Do not introduce
neon accents, gradients, outlines or shadows to the symbol. The monochrome and
reversed variants preserve the same geometry and transparent underpass.

The wordmark uses the system font stack `Avenir Next, Segoe UI, Arial, sans-serif`
with medium weight and restrained tracking. No font file is distributed or
embedded; installed fallbacks may change letter metrics slightly. Keep the
wordmark on one line and in one color. In the banner, the descriptor and quiet
ecosystem attribution share a text alignment axis.

## Edit and display

Edit the XML directly or use a standards-compliant SVG editor. Preserve each
`viewBox`, accessible `title` and `desc`, license comment and the underpass mask.
Scale proportionately. Keep at least one band-width of clear space around the
visible symbol; do not add an avatar tile or crop into the tips. Use the reversed
asset on dark backgrounds instead of placing the forest/jade version there.
At 16 pixels the silhouette is the primary identifier; the crossing becomes
clearer at 24 pixels and above.

The banner has an opaque white background and works within light or dark pages.
Its wordmark remains readable in a 480-pixel preview; secondary attribution is
intentionally subordinate. Display architecture diagrams at 900 pixels wide or
allow opening at native size. Their technical content is independent of the
identity and uses an opaque neutral background for readability.

## Reproduce a render

Open an SVG in a browser to view the original. For explicit raster dimensions,
use an installed Node.js environment with the optional `sharp` package. Record
the renderer version and installed fallback fonts:

```sh
node -p 'require("sharp").versions'
node -e 'require("sharp")(process.argv[1]).resize({width: Number(process.argv[3])}).png().toFile(process.argv[2])' \
  assets/weave-logo.svg /tmp/weave-symbol.png 256
node -e 'require("sharp")(process.argv[1]).resize({width: Number(process.argv[3])}).png().toFile(process.argv[2])' \
  assets/banner.svg /tmp/weave-banner.png 960
```

Change the input, destination and width for the monochrome/reversed assets,
small symbols (16/24/32/64), or diagrams (900). These optional authoring tools
are not product runtime dependencies. The SVG source requires no generation step.

Before shipping an edit, inspect every symbol variant at 16/24/32/64/256 pixels
on its intended background, plus the banner at 960 and 480 pixels. Check clear
space, silhouette, crossing gaps, text clipping and optical alignment. For browser
screenshots, wait for image decoding and fonts, then a completed paint; decoding
alone does not prove the captured pixels contain the interior artwork. Inspect
the actual saved PNGs and verify meaningful foreground pixels in each image.
Use unique evidence filenames for each attempt so a failed capture remains
distinguishable from a corrected one. Keep previews outside the publication tree.

## Technical diagram catalog

The [architecture guide](architecture.md) embeds source-grounded system context,
compiler lifecycle, worker recovery, identity/secret boundary, integration delivery,
and three selected ER views. The selected ER views show real scoped foreign keys;
additional logical references and operational retention rules remain in prose.
These technical diagrams do not modify the approved visual identity.

Render any SVG using the command above, changing the input to the corresponding
`docs/diagrams/` path. Inspect at 900–960 pixels wide on light/dark surroundings;
provide a full-size link and horizontal space for the ER views. On narrow screens,
open the original SVG at its readable width rather than shrinking dense labels.
The small license/Python/alpha badges state current source facts only; there is no
fabricated CI, release, coverage or live-provider badge.
