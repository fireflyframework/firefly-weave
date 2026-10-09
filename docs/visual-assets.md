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

# Use the Firefly Weave brand assets and draw diagrams

Use this page when you place the Firefly Weave logo in a page, document or slide,
when you need a brand color or font, or when you draw or change a diagram in this
documentation. It tells you who owns each asset, which file to use on which
background, the rules every technical diagram follows, how to regenerate the
artwork, and how to check a change at its real size. It takes about ten minutes
to read.

## Who owns what

| Asset | Owner | License |
| --- | --- | --- |
| The Firefly name, the firefly wordmark with its spark and chevron, and the Firefly icon | Firefly Software Solutions Inc. | Trademarks and proprietary artwork, used by the Firefly Software Foundation with permission; not licensed under the Apache License 2.0 (see [NOTICE](../NOTICE)) |
| The Weave name and the Weave wordmark, the woven w and "eave", including the w alone | Firefly Software Foundation | Trademarks. The three master files are first-party and Apache-2.0 as copyright works, but the license grants no trademark rights in the name or the drawing (Apache License 2.0, section 6; see [NOTICE](../NOTICE)) |
| The Firefly Weave logo, which combines the Firefly logo and the Weave wordmark | Both owners | Recorded as third-party wherever it appears, because the Firefly part is not Apache-2.0 |
| Firefly Weave code, documentation, diagrams and badges | Firefly Software Foundation | Apache License 2.0 |
| The Manrope typeface | The Manrope Project Authors | SIL Open Font License 1.1 ([OFL.txt](../assets/fonts/manrope/OFL.txt)) |
| Studio icons, derived from Lucide | Lucide contributors | ISC, with MIT for the parts from Feather ([license](../studio/public/licenses/lucide-LICENSE.txt)) |

The [source inventory](contributing/source-inventory.toml) records every file
that contains Firefly marks as a third-party asset, and names both owners where
the file also holds the Weave wordmark. The SVG files among them carry a
trademark comment instead of the Apache-2.0 header; PNG and icon files cannot
hold a comment, so the inventory is their record. The three wordmark masters
carry the Apache-2.0 header and a trademark line.

## Choose the right asset

![The Firefly Weave logo in paper on charcoal](../assets/brand/weave-lockup-reversed.svg#gh-dark-mode-only)

![The Firefly Weave logo in charcoal, shown on a paper panel](../assets/brand/weave-lockup-color.svg#gh-light-mode-only)

| Asset | Use it for | Size |
| --- | --- | --- |
| [Logo, reversed](../assets/brand/weave-lockup-reversed.svg) | Studio, the desktop launch page, the banner, the social preview and the DMG: paper ink and an amber strand for charcoal and every dark surface | 1368.66 × 248.25 units |
| [Logo, color](../assets/brand/weave-lockup-color.svg) | Light surfaces outside the product, such as slides and print: charcoal ink and a gold strand for paper and white | 1368.66 × 248.25 units |
| [Small logo, reversed](../assets/brand/weave-lockup-small-reversed.svg) | The documentation header and other dark placements where the Firefly part is 48 to 80 pixels wide | 1368.66 × 248.25 units |
| [Weave wordmark](../assets/brand/weave-wordmark.svg) and [small wordmark](../assets/brand/weave-wordmark-small.svg) | The drawn w and "eave" that the logo and the small logo are built from; sources for the generator, not for placing on a page | 535.26 × 116 units |
| [The w alone](../assets/brand/weave-w.svg) | A Weave-only glyph, 32 pixels or larger; no placement uses it yet | 56 × 56 |
| [Firefly icon](../assets/brand/firefly-icon.svg) | The documentation favicon and the Windows and Linux application icons | 56 × 56 tile |
| [Small Firefly icon](../assets/brand/firefly-icon-small.svg) | Icon sizes below 32 pixels, with a solid chevron | 56 × 56 tile |
| [Firefly mark](../assets/brand/firefly-mark.svg) | The collapsed Studio sidebar, on a transparent background | 56 × 56 |
| [Social preview](../assets/brand/social-preview.png) | The GitHub repository card; an administrator uploads it in the repository settings | 1280 × 640 pixels |
| [Banner](../assets/banner.svg) | The README and the documentation home; it carries its own charcoal ground | 1120 × 280 pixels |
| Badges for the [license](../assets/badges/license.svg), [Python version](../assets/badges/python.svg), and [maturity](../assets/badges/alpha.svg) | Small labels that state facts from the source tree | 28 pixels high |
| [macOS icon master](../desktop/artwork/app-icon-macos.svg) and the [DMG background](../desktop/artwork/dmg-background.svg) | The desktop app's icon and installer window | 1024 × 1024 and 720 × 480 pixels |
| Technical diagrams in `docs/diagrams/` | Explaining how Weave works; the [visual guide](visual-guide.md) lists every one | 960 or 1120 pixels wide |

## The Firefly Weave logo

The logo is the official Firefly compact logo, a hairline separator and the Weave
wordmark, built with the Firefly co-brand geometry; it reads "firefly› │ weave".
Its unit, X, is the Firefly x-height: the height of the chevron.

- **Separator.** It sits 1X after the chevron, is 1.25 times the height of the
  Firefly logo, is centered on the x-height, and is 0.04X wide, in the ink color
  at 55% opacity.
- **Weave wordmark.** It is drawn, never typed: a lowercase w woven from two
  strands, then "eave" as unaltered Manrope Medium (500) outlines at the size and
  tracking of the firefly wordmark. The w stands on the x-height, like the Firefly
  letters, and its strands run at the slant of the v in "eave". The wordmark
  starts 1X after the separator on the Firefly baseline. Every logo file holds
  paths only, no live text.
- **The strand.** The second strand passes over the first, which stops short of it
  by the crossing gap: 10 units in the logo and 14 in the small logo, so the
  crossing stays open on a 1x screen. Do not narrow the gap. The strand is the
  only amber in the wordmark, and it carries at most 0.70 times the amber of the
  Firefly logo beside it.
- **Colors.** Paper ink (`#F3F1EB`) with an amber strand (`#FFB34A`) on charcoal
  and every dark surface; charcoal ink (`#10110F`) with a gold strand (`#855414`)
  on paper and white, because amber is only 1.58:1 on paper. The spark and chevron
  stay amber in both.
- **Clear space.** Keep 1X free on every side. Layouts reserve it; the files do
  not include it.
- **Small logo.** The same construction with a solid chevron, no light trail and
  the small wordmark, whose crossing gap is wider.

The generator prints four measurements for every logo file, and all three share
one box: FW = 619.4 units, lockup width = 1368.66 units, lockup height = 248.25
units, ratio FW / width = 0.4526. The Firefly part of a placement is its rendered
width times that ratio, so a placement is checked by arithmetic:

| Placement | Rendered size | Firefly part | File |
| --- | --- | --- | --- |
| Studio sidebar, expanded | 192 pixels wide, 35 high | 86.9 pixels | Logo |
| Studio pairing pages | 240 pixels wide | 108.6 pixels | Logo |
| Desktop launch page | 240 pixels wide, 44 high | 108.6 pixels | Logo |
| Documentation header | X = 11 pixels: 139.4 × 25.3 pixels | 63.1 pixels | Small logo |
| README banner | X = 32 pixels: 405.5 pixels wide | 183.5 pixels | Logo |
| Social preview | X = 48 pixels: 608.3 pixels wide | 275.3 pixels | Logo |
| DMG background | X = 16 pixels: 202.8 pixels wide | 91.8 pixels | Logo |

**Minimum sizes.** Use the logo only when its Firefly part is at least 80 pixels
wide, which makes the logo at least 177 pixels wide. Use the small logo when the
Firefly part is 48 to 80 pixels wide, at least 107 pixels for the whole logo, and
the Firefly icon (16 pixels at least) below that. The whole logo is never narrower
than 160 pixels, except the small logo.

**Never** redraw, recolor, retype or rearrange the logo or the wordmark, put any
part of the wordmark other than the w's strand in amber, use the amber strand on
paper, place the logo inside a diagram, or reproduce it as text or ASCII art. CLI
help prints the product name, Firefly Weave, instead.

## Colors

Studio and the desktop app use one dark set of semantic tokens in
`studio/src/styles.css`, and components use only those tokens. The anchors are:

| Token | Value | Use |
| --- | --- | --- |
| `--bg` | `#10110F` (charcoal) | App background, sidebar and page |
| `--surface` | `#1A1B17` | Panels, cards, dialogs |
| `--raised` | `#1F201C` | Menus, popovers, toasts, node cards |
| `--text` | `#F3F1EB` (paper) | Text, 16.76:1 on charcoal |
| `--muted` | `#BFB8AB` (stone) | Secondary text |
| `--accent` | `#FFB34A` (amber) | The primary action, focus, selection and the live run |
| `--link` | `#FACC8A` | Links and tertiary buttons |

Amber is never a status color, a large background or decoration, and never
carries text on a light surface. The documentation site uses one dark scheme on
charcoal. Diagrams, the exported workflow graph and the Swagger UI body of the
API explorer stay light, on paper.

Diagrams use this light palette:

| Role | Value |
| --- | --- |
| Text and dark fills | Ink `#272820` |
| Secondary text, edges, icons and arrowheads | Muted `#62645B` |
| Accent text, number discs and the Takeaway heading | Gold `#855414`, with white numerals |
| Data labels | Slate `#4A5D65` |
| Diagram ground | Paper `#F3F1EB` |
| Bands and panels | Band `#EAE7DF` |
| Third-party systems | `#E6E8E6` |
| Hairlines | Line `#D8D4CA` |
| Card strokes | Stone `#BFB8AB` |
| Cautions | Warm tint `#FFF0D8`, with gold strokes and text |
| Warm strokes | Amber line `#F0A33C`, never text |
| Nodes | White `#FFFFFF` |

## Type and icons

Studio and this website use Manrope from their own files, never from a font
service. Artwork outlines its Manrope text, so it renders the same everywhere.
Diagrams use Arial (`Arial, Helvetica, sans-serif`) and Menlo or Consolas for
code: a page loads a diagram as an image, which cannot use the website's font.

Studio icons are Lucide drawings rendered through `<weave-icon>`. To add one,
add its name to the map in [the icon generator](../studio/scripts/build-icons.mjs);
never paste inline SVG.

## Draw or change a technical diagram

A diagram earns its place when it answers one question a reader has, such as
"what happens between my request and the result?". Every diagram in
`docs/diagrams/` follows these rules; `scripts/check_docs.py` enforces the
structural ones and `scripts/recolor_diagrams.py --check` the colors.

| Rule | Why |
| --- | --- |
| Plain, editable SVG, with the Apache-2.0 license comment first | The SVG is the editable source, and the source check requires the header |
| `role="img"`, a `<title>`, a `<desc>`, and a `viewBox` on the root element | Screen readers announce the title and read the description; the `viewBox` lets the image scale. Missing pieces fail the documentation check |
| No `<script>`, `<foreignObject>`, or embedded `<image>` | Diagrams stay inert and self-contained; any of these fails the documentation check |
| Width 1120 pixels for new diagrams; older diagrams are 960 | Text stays readable when the page shows the image at full width |
| Arial for text, Menlo or Consolas for code | These fonts are installed almost everywhere, so labels do not reflow |
| Text at least 16 pixels | Labels stay legible at 100% zoom |
| The diagram palette above, on a paper ground | Diagrams look like one family on their paper panels; any other color fails the recolor check |
| Most diagrams end with a "Takeaway" band, with no artwork | One sentence tells the reader what to remember |
| No Firefly logo inside a diagram | The marks stay in their own asset files |
| A `<desc>` that states everything the picture shows | Readers who cannot see the image get the same facts |

**Every label must be true.** Check each command, path, field, and error code in
a diagram against the code, exactly as you would in prose. A diagram must not
promise a guarantee or a verification that the code and tests do not provide.

**Convert an older diagram.** Run `python3 scripts/recolor_diagrams.py` to map
the earlier palette to this one; it fails, and writes nothing, when it meets a
color it does not know.

**Embed it the same way everywhere.** In the page, put the image, then a
one- or two-sentence reading guide, then a full-size link. Many pages start the
reading guide with **How to read this diagram:**, as in this pattern:

```markdown
![What the diagram answers](../diagrams/your-diagram.svg)

**How to read this diagram:** Follow the numbered arrows from left to right. …

[Open diagram at full size](../diagrams/your-diagram.svg)
```

Then add a row for the new diagram to the [visual guide](visual-guide.md), which
connects each figure to the reader's question and the page that explains it.

## Regenerate the brand assets

Firefly artwork is generated, never edited by hand. The wordmark is a drawing:
to change it, edit its three masters (`weave-wordmark.svg`,
`weave-wordmark-small.svg` and `weave-w.svg` in `assets/brand/`) and run the
generator again. You need access to the private Firefly Brand Kit 2.0.0 and
Node.js 22.12 or later.

1. Clone the kit outside this repository. In the kit, install its packages and
   build its logo pack:

    ```sh
    # Install the kit's locked packages without download scripts.
    npm ci --ignore-scripts
    # Build the logo pack; the final CMYK PDF step needs Python with reportlab and may fail.
    npm run build -- --only logo
    # Expected: no output, so no drawing carries the old paper value.
    grep -ril f3f3e9 dist/Firefly-Brand-Kit/02-Logo/svg
    ```

2. From the kit root, run the composer with the path of your Firefly Weave
   checkout. It imports the kit's logo and type modules at run time, reads the
   wordmark masters from your checkout, writes the generated files in the asset
   table above, and prints the measurements:

    ```sh
    node /path/to/firefly-weave/scripts/brand/weave-lockup.mjs --out /path/to/firefly-weave
    ```

3. From your Firefly Weave checkout, rebuild the desktop icons with the Tauri
   CLI, then copy `icon.icns` from the first folder and `32x32.png`,
   `128x128.png`, `128x128@2x.png` and `icon.png` from the second into
   `desktop/src-tauri/icons/`. The composer writes `icon.ico` itself, with the
   solid chevron below 32 pixels.

    ```sh
    npm --prefix desktop ci
    npm --prefix desktop run tauri -- icon "$PWD/desktop/artwork/app-icon-macos.svg" -o "$HOME/.cache/firefly-weave/icons-macos"
    npm --prefix desktop run tauri -- icon "$PWD/assets/brand/firefly-icon.svg" -o "$HOME/.cache/firefly-weave/icons-full"
    ```

4. Review `git status`, check each changed file at its real size, and keep the
   files' inventory entries. Kit scripts, data and icons never enter this
   repository; only the generated files do.

## Check a change at its real size

XML validation cannot show you overlapping labels or a misleading arrow, so look
at the pixels. Open the SVG in a browser at 100% zoom, or render a PNG at the
diagram's native size with a headless Chromium. This example uses
`chrome-headless-shell`, the screenshot-only browser that Playwright downloads
with Chromium into its browser cache (`~/Library/Caches/ms-playwright` on macOS).
It is not on your `PATH`: run it by its full path in that cache, or use another
Chromium-based browser with the same flags.

```sh
# Render the diagram at its viewBox size; read the width and height from the SVG's viewBox.
chrome-headless-shell --headless --screenshot=/tmp/system-context.png \
  --window-size=960,940 "file://$PWD/docs/diagrams/system-context.svg"
```

Expected: the browser reports `… bytes written to file /tmp/system-context.png`,
and the PNG is exactly 960 × 940 pixels. On macOS it may also print
`CVDisplayLinkCreateWithCGDisplay failed` lines; they do not affect the image.
Open the PNG and check it.

**What to check before you ship an edit:**

- **Diagrams:** read every label at the native width and at about 900 pixels, on
  the paper panel of the dark website. Look for clipped or overlapping text,
  arrows that cross labels, and a reading guide that matches the picture.
- **Logo and icons:** inspect the logo at each placement in the table above and
  the icons at 16, 24, 32, 64, and 256 pixels on their intended backgrounds.
  Check the clear space, that the small sizes use the solid chevron, and that the
  woven w's crossing stays open at its smallest placement.
- **Banner:** inspect it at 960 and 480 pixels.
- **Screenshots:** wait until fonts and images have loaded before you capture,
  and confirm that the saved PNG really shows the artwork. Give each attempt its
  own file name so a failed capture is never confused with a corrected one, and
  keep previews outside the repository.

## Next steps

- [Visual guide](visual-guide.md): find the diagram that answers your question.
- [Build and maintain the documentation website](contributing/documentation-site.md):
  preview a page and follow the writing conventions.
- [Architecture](architecture.md): see the system diagrams in context.
