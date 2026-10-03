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

# Use the Weave logo, Lumi, and diagrams

Use this page when you place the Weave logo in a document or slide, or when you
draw or change a diagram in this documentation. It tells you which file to use
on which background, the colors and type to keep, the rules every technical
diagram follows, and how to check a change at its real size. It takes about ten
minutes to read. To edit, you need a text editor or a standards-compliant SVG
editor, and a Chromium-based browser to check the result.

The brand assets are distributed under the Apache License 2.0. Logos and
technical diagrams use editable SVG source, with no scripts, remote fonts,
embedded raster images, or `foreignObject`. Lumi is an image-generated PNG
with a transparent background. The checked-in image is the master asset;
building Studio or the documentation does not call an image-generation service.

## Choose the right asset

| Asset | Use it for | Native size |
| --- | --- | --- |
| [Weave symbol](../assets/weave-logo.svg) | The forest and jade mark on white or light neutral surfaces | 256 × 256 |
| [Monochrome symbol](../assets/weave-logo-mono.svg) | One-color reproduction on light surfaces | 256 × 256 |
| [Reversed symbol](../assets/weave-logo-reversed.svg) | The white mark on dark surfaces; the website header uses it | 256 × 256 |
| [Banner](../assets/banner.svg) | The README and project overview; it has an opaque white background | 1120 × 280 |
| [Lumi PNG](../assets/lumi.png) | Studio, documentation, slides, and diagram companion artwork; see [Meet Lumi](#meet-lumi) | 1254 × 1254, transparent |
| Badges for the [license](../assets/badges/license.svg), [Python version](../assets/badges/python.svg), and [maturity](../assets/badges/alpha.svg) | Small labels that state facts from the source tree. There is no CI, release, coverage, or live-provider badge | 28 pixels high |
| Technical diagrams in `docs/diagrams/` | Explaining how Weave works; the [visual guide](visual-guide.md) lists every one | 960 or 1120 pixels wide |

## The symbol, colors, and type

The Weave symbol is a compact woven W: two diagonal bands with a transparent
underpass. Flat ends and an even band weight keep it readable at small sizes.
Forest and a single jade accent connect it to the Firefly family without using
PyFly's mark. The banner pairs the symbol with a one-color wordmark, the
descriptor "Workflow orchestration and integration", and a quieter line,
"Part of the Firefly Framework ecosystem".

| Color | Value | Used for |
| --- | --- | --- |
| Forest | `#173D34` | The wordmark and the primary band |
| Jade | `#367D68` | The single accent band |
| Slate | `#62706A` | Secondary text in the banner |
| White | `#FFFFFF` | The banner background |
| Mist | `#EEF4F0` | An optional neutral surround |

Do not add neon accents, gradients, outlines, or shadows to the symbol. The
monochrome and reversed variants keep the same geometry and the transparent
underpass.

The wordmark uses the system font stack `Avenir Next, Segoe UI, Arial, sans-serif`
at semibold weight (600) with restrained tracking. No font file is distributed or
embedded, so the installed fallback fonts may change letter widths slightly. Keep
the wordmark on one line and in one color. In the banner, the descriptor and the
ecosystem line share one left alignment.

## Place the logo and banner

- **Keep the proportions.** Scale proportionately and keep each `viewBox`.
- **Leave room.** Keep at least one band width of clear space around the visible
  symbol. Do not add an avatar tile or crop into the tips.
- **Match the background.** Use the reversed symbol on dark backgrounds instead
  of placing the forest and jade version there.
- **Mind small sizes.** At 16 pixels the silhouette identifies the symbol; the
  crossing becomes clear at 24 pixels and above.
- **Show the banner at a readable width.** Its wordmark stays readable in a
  480-pixel preview; the ecosystem line is intentionally subordinate. Its opaque
  white background works on light and dark pages.

When you edit the XML directly or in an SVG editor, keep each `viewBox`, the
accessible `<title>` and `<desc>`, the license comment, and the underpass mask.

## Draw or change a technical diagram

A diagram earns its place when it answers one question a reader has, such as
"what happens between my request and the result?". Every diagram in
`docs/diagrams/` follows these rules; `scripts/check_docs.py` enforces the
structural ones.

| Rule | Why |
| --- | --- |
| Plain, editable SVG, with the Apache-2.0 license comment first | The SVG is the editable source, and the source check requires the header |
| `role="img"`, a `<title>`, a `<desc>`, and a `viewBox` on the root element | Screen readers announce the title and read the description; the `viewBox` lets the image scale. Missing pieces fail the documentation check |
| No `<script>`, `<foreignObject>`, or embedded `<image>` | Diagrams stay inert and self-contained; any of these fails the documentation check |
| Width 1120 pixels for new diagrams; older diagrams are 960 | Text stays readable when the page shows the image at full width |
| Arial for text (`Arial, Helvetica, sans-serif`), Menlo or Consolas for code | These fonts are installed almost everywhere, so labels do not reflow |
| Text at least 16 pixels | Labels stay legible at 100% zoom |
| Forest `#173D34` and jade `#367D68` on mist `#EEF4F0`, with warm `#FFF7E5` for cautions | Diagrams look like one family and keep enough contrast |
| Most diagrams end with a "Lumi's takeaway" band | One sentence tells the reader what to remember |
| A `<desc>` that states everything the picture shows | Readers who cannot see the image get the same facts |

**Every label must be true.** Check each command, path, field, and error code in
a diagram against the code, exactly as you would in prose. A diagram must not
promise a guarantee or a verification that the code and tests do not provide.

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

To render the logo variants at exact pixel sizes instead, you can use Node.js
with the optional [sharp](https://sharp.pixelplumbing.com/) package. It is an
authoring tool, not a product dependency. Record the renderer version, because
installed fallback fonts change the result:

```sh
# Record the renderer and library versions used for this render.
node -p 'require("sharp").versions'
# Render the symbol at 256 pixels wide and the banner at 960 pixels wide.
node -e 'require("sharp")(process.argv[1]).resize({width: Number(process.argv[3])}).png().toFile(process.argv[2])' \
  assets/weave-logo.svg /tmp/weave-symbol.png 256
node -e 'require("sharp")(process.argv[1]).resize({width: Number(process.argv[3])}).png().toFile(process.argv[2])' \
  assets/banner.svg /tmp/weave-banner.png 960
```

Expected: the first command prints each bundled library with its version; the
PNGs are 256 × 256 and 960 × 240 pixels. Change the input, output, and width for the
other variants and sizes.

**What to check before you ship an edit:**

- **Diagrams:** read every label at the native width and at about 900 pixels.
  Look for clipped or overlapping text, arrows that cross labels, and a reading
  guide that matches the picture. On narrow screens, readers should open the
  full-size SVG instead of a shrunken image.
- **Symbol variants:** inspect each at 16, 24, 32, 64, and 256 pixels on its
  intended background. Check the clear space, silhouette, crossing gaps, and
  optical alignment.
- **Banner:** inspect it at 960 and 480 pixels.
- **Screenshots:** wait until fonts and images have loaded before you capture,
  and confirm that the saved PNG really shows the artwork. Give each attempt its
  own file name so a failed capture is never confused with a corrected one, and
  keep previews outside the repository.

## Meet Lumi

![Lumi, the Firefly Weave guide](../assets/lumi.png)

**Lumi** is Weave's firefly guide: a sculpted forest-green body, translucent
mint wings, and an amber lantern. The image uses soft lighting and a transparent
background so it sits naturally on white, mist, or dark forest surfaces.

Use the [PNG master](../assets/lumi.png) for new artwork. Studio's pairing and
home screens and the documentation introductions use this image. Existing
self-contained SVG diagrams and CLI graph exports retain the earlier vector
illustration; those are separate assets, not a vector version of this image.
The official Weave symbol remains the application icon and primary product mark.

### Reuse Lumi in a page, slide, or diagram

1. Download the PNG master above. Its alpha channel provides transparency;
   there is no white rectangle to remove.
2. Keep the square aspect ratio. Leave space around both antennae and the wings,
   and avoid circular crops that cut through them.
3. Use a displayed width of 96–240 pixels for an introduction or diagram
   companion. At small toolbar sizes, use the Weave symbol instead: Lumi's face
   and wings need room to remain legible.
4. Place Lumi beside a short takeaway, outside the boxes and arrows that explain
   the system. Do not use the character as a status, permission, or execution
   indicator.
5. Keep a single shared PNG reference in web pages. For the documentation's SVG
   diagrams, place it next to the diagram in the surrounding Markdown instead
   of embedding a copy of the raster in every SVG. This keeps diagrams editable
   and lets the browser cache the image once.

For a guide under `docs/guides/`, use this pattern:

```markdown
<!-- Keep the mascot separate so the technical diagram stays readable and editable. -->
![Lumi, the Firefly Weave guide](../../assets/lumi.png){ .lumi-guide }

Lumi's takeaway: review the proposed change before applying it.

## Follow the process

![How a suggestion becomes a reviewed draft](../diagrams/lumi-review-lifecycle.svg)
```

The [Lumi assistant guide](guides/lumi.md) shows the image alongside the
explanation and review diagram. In Studio, `studio/public/assets/lumi.png` is
an identical copy of the master; update both together when the artwork changes.
Machine JSON output contains no branding. CLI help uses the official logo;
progress animation settings are in the [CLI reference](reference/cli.md#local-platform-commands).

## Next steps

- [Visual guide](visual-guide.md): find the diagram that answers your question.
- [Build and maintain the documentation website](contributing/documentation-site.md):
  preview a page and follow the writing conventions.
- [Architecture](architecture.md): see the system diagrams in context.
