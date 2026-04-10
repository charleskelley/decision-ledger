# Deep Teal & Charcoal

## Design Palette & Usage Guidelines

### Why teal

Teal occupies a distinctive gap in AI branding. Most AI companies cluster around
blue (Google, Meta AI), purple (AI21, Cohere), black/white minimalism (OpenAI,
Mistral), or warm earth tones (Anthropic). Teal reads as both "technology"
and "trust" without defaulting to the cold-blue cliché that dominates the space.

For DecisionLedger specifically, teal carries associations of verification,
system health, and clinical precision — all directly relevant to a system that
evaluates, verifies, and records AI decisions. In healthcare contexts, teal
often carries clinical trust associations. In regulated enterprise contexts, the
charcoal grounding communicates seriousness.

### Why charcoal over pure black

Pure black (`#000000`) against white creates maximum contrast but feels sterile
and stark. The warm charcoal (`#1c2024`) has a subtle blue-gray undertone that
pairs naturally with the teal accent and creates a more considered, less generic
feel. On dark themes, the corresponding light gray (`#e6eaed`) avoids the
harshness of pure white text on dark backgrounds.

### Dark/light theme parity

Every color in the palette has a light-theme and dark-theme variant. The system
was designed so that switching themes feels like the same brand in different
lighting — not two different brands. The teal accent shifts slightly brighter on
dark backgrounds (`#14b8a6` vs`#0d9488`) to maintain equivalent visual weight.

## Color Tokens

### Primary Palette

| Token          | Light Theme | Dark Theme | Role                                                                                |
|----------------|-------------|------------|-------------------------------------------------------------------------------------|
| **Structure**  | `#1c2024`   | `#e6eaed`  | Primary text, headings, logo structural strokes, high-emphasis UI elements          |
| **Accent**     | `#0d9488`   | `#14b8a6`  | Decision bar in logo, links, interactive elements, status indicators, accent labels |
| **Background** | `#f6f7f8`   | `#111416`  | Page background, canvas                                                             |
| **Surface**    | `#e9ecef`   | `#1a1e21`  | Cards, panels, code blocks, elevated containers                                     |
| **Border**     | `#d5d9dd`   | `#262c30`  | Dividers, card borders, table rules, subtle separators                              |
| **Muted**      | `#6b7b86`   | `#6b7b86`  | Secondary text, captions, timestamps, metadata (same in both themes)                |

### Extended Palette

These colors are derived from the primary palette for specific UI needs.

| Token                | Light Theme             | Dark Theme               | Role                                                       |
|----------------------|-------------------------|--------------------------|------------------------------------------------------------|
| **Accent Subtle**    | `rgba(13,148,136,0.08)` | `rgba(20,184,166,0.08)`  | Accent-tinted backgrounds for tags, badges, callouts       |
| **Accent Hover**     | `#0b7e74`               | `#1cc9b4`                | Hover state for accent-colored interactive elements        |
| **Structure Subtle** | `rgba(28,32,36,0.06)`   | `rgba(230,234,237,0.06)` | Very light emphasis backgrounds, zebra-striped rows        |
| **Danger**           | `#c0392b`               | `#e05545`                | Error states, destructive actions, failed evaluations      |
| **Success**          | `#1a7a5a`               | `#2dcc8a`                | Passed evaluations, successful deployments, healthy status |
| **Warning**          | `#b8860b`               | `#daa520`                | Caution states, degraded performance, threshold warnings   |

### Infographic Palette
 
These colors exist for data visualizations, architecture diagrams, pipeline flow
illustrations, decision distribution charts, and any context where more than
three categorical series need to be visually distinct. They are designed to:
 
1. Remain distinguishable at small sizes (legend swatches, thin flow lines, 12px
   labels).
2. Maintain sufficient contrast against both **Background** and **Surface** in
   each theme.
3. Avoid collision with the existing semantic colors (Accent/teal, Success/green,
   Danger/red, Warning/gold) while harmonizing with the teal-charcoal brand
   feel.
4. Degrade gracefully for the three most common forms of color vision deficiency
   (protanopia, deuteranopia, tritanopia) by relying on luminance separation in
   addition to hue.
 
#### Categorical Series
 
Use these when charting unranked categories — pipeline stages, decision types,
scenario classes, evaluation dimensions, or any set of items that need color
differentiation without implying order or severity.
 
| Token           | Light Theme | Dark Theme | Suggested mapping                                                  |
|-----------------|-------------|------------|--------------------------------------------------------------------|
| **Series 1**    | `#0d9488`   | `#14b8a6`  | Primary/default series — reuses Accent teal for brand continuity   |
| **Series 2**    | `#5b6abf`   | `#7c8ae6`  | Indigo — policy retrieval, RAG components, study/learning contexts |
| **Series 3**    | `#b45dc9`   | `#cf7ee6`  | Violet — GenAI gate, LLM-driven stages, model inference           |
| **Series 4**    | `#c9622e`   | `#e68a52`  | Burnt orange — enforcement, action, downstream effects            |
| **Series 5**    | `#2e8b8b`   | `#45b8b8`  | Cyan — online features, real-time computation, streaming           |
| **Series 6**    | `#8c6d3f`   | `#bfa06a`  | Ochre — historical/audit, replay, Decision Bundle references      |
 
If a visualization requires more than six series, derive additional values by
taking any series color at 65% opacity on the Surface background. Do not invent
new hues outside this set — the constraint is intentional.
 
#### Sequential Ramps
 
Use these for ordered data — confidence scores, latency percentiles, risk
gradients, or any metric that progresses from low to high. Each ramp provides
five stops from lightest to darkest. Intermediate values can be interpolated
linearly in LCH color space (not HSL — HSL interpolation produces muddy
midpoints with these hues).
 
**Teal ramp** (default for single-metric visualizations):
 
| Stop   | Light Theme | Dark Theme | Typical use                        |
|--------|-------------|------------|------------------------------------|
| **100** | `#d5f0ed`   | `#0c2e2b`  | Floor / minimum / baseline         |
| **300** | `#82d4cb`   | `#1a6b62`  | Low-mid range                      |
| **500** | `#0d9488`   | `#14b8a6`  | Midpoint — matches Accent          |
| **700** | `#086b62`   | `#5dd8ca`  | High-mid range                     |
| **900** | `#04403b`   | `#a1ede4`  | Ceiling / maximum / critical value |
 
**Warm ramp** (for a second metric axis or when teal is already in use):
 
| Stop   | Light Theme | Dark Theme | Typical use                        |
|--------|-------------|------------|------------------------------------|
| **100** | `#f5e6d5`   | `#2e2015`  | Floor / minimum / baseline         |
| **300** | `#e0b482`   | `#7a5a30`  | Low-mid range                      |
| **500** | `#b8860b`   | `#daa520`  | Midpoint — matches Warning/gold    |
| **700** | `#8a6408`   | `#e6be4a`  | High-mid range                     |
| **900** | `#5c4205`   | `#f0da8a`  | Ceiling / maximum / critical value |
 
#### Diverging Ramp
 
Use when a metric has a meaningful neutral center and diverges in two directions —
decision confidence (low ← uncertain → high), drift magnitude (negative ←
stable → positive), or any bipolar scale.
 
| Stop   | Light Theme | Dark Theme | Position                           |
|--------|-------------|------------|------------------------------------|
| **−2** | `#c0392b`   | `#e05545`  | Strong negative — reuses Danger    |
| **−1** | `#d9887f`   | `#a04a3d`  | Moderate negative                  |
| **0**  | `#e9ecef`   | `#1a1e21`  | Neutral center — reuses Surface    |
| **+1** | `#7ac4bc`   | `#2a7a72`  | Moderate positive                  |
| **+2** | `#0d9488`   | `#14b8a6`  | Strong positive — reuses Accent    |
 
The center of the diverging ramp is the Surface color, which means the ramp
reads as "emerging from neutral" in both directions. If the diverging metric
is not inherently positive/negative (e.g., comparing two models rather than
good/bad), swap the Danger end for Series 2 (indigo) to avoid implying judgment.
 
#### Infographic Backgrounds & Containers
 
| Token                   | Light Theme             | Dark Theme               | Role                                                             |
|-------------------------|-------------------------|--------------------------|------------------------------------------------------------------|
| **Chart Background**    | `#ffffff`               | `#141618`                | Dedicated chart/diagram canvas — slightly offset from page Background to frame the visual |
| **Chart Grid**          | `rgba(28,32,36,0.07)`  | `rgba(230,234,237,0.07)` | Axis gridlines, reference lines — barely visible, never competing with data |
| **Chart Axis**          | `#6b7b86`               | `#6b7b86`                | Axis lines and tick labels — reuses Muted                        |
| **Annotation**          | `#1c2024`               | `#e6eaed`                | Callout text, value labels on data points — reuses Structure     |
| **Annotation Subtle**   | `rgba(28,32,36,0.50)`  | `rgba(230,234,237,0.50)` | Secondary annotations, non-critical data labels                  |
 
## Usage Guidelines for Infographic Tokens
 
### Choosing between categorical and sequential
 
If the data categories have no inherent order (pipeline stages, scenario types,
evaluation dimensions), use the **Categorical Series** colors. If the data
represents a progression (risk scores, latency, confidence), use a **Sequential
Ramp**. If you find yourself using a sequential ramp for categorical data, the
chart type is probably wrong.
 
### Series assignment consistency
 
Once a concept is assigned a series color within a document, dashboard, or slide
deck, that assignment should be maintained across every chart in the same
context. "Policy RAG" should not be Series 2 (indigo) in one chart and Series 4
(burnt orange) in the next. The suggested mappings in the table above are
starting points — the rule is internal consistency, not rigid adherence to the
suggested associations.
 
### Combining with semantic colors
 
The semantic colors (Danger, Success, Warning) already carry meaning and should
not be reused as arbitrary categorical colors. If a chart needs to show both
status (pass/fail/caution) and category (pipeline stage), use semantic colors for
status and series colors for category. Never mix the two roles on the same axis.
 
### Accessible pattern use
 
For any context where color alone distinguishes data (particularly in printed or
low-resolution formats), pair each series with a distinct pattern or marker
shape: Series 1 = solid / circle, Series 2 = dashed / square, Series 3 = dotted /
diamond, Series 4 = dash-dot / triangle-up, Series 5 = short-dash / triangle-down,
Series 6 = long-dash-short-dash / cross. These pairings are recommendations;
the requirement is that each series is distinguishable without color.
 
### Opacity layering in dense charts
 
When data series overlap (stacked areas, overlapping distributions), layer the
series colors at 20% opacity for fills and 100% for strokes. This prevents
darker series from visually dominating and maintains readability where regions
overlap. On dark backgrounds, increase fill opacity to 25% — the lower ambient
luminance otherwise makes 20% fills invisible.
 
---

## Usage Guidelines

### Text

**Structure** (`#1c2024` / `#e6eaed`) is the default text color. Use it for all
body text, headings, navigation labels, and any text that needs to be read as
primary content.

**Muted** (`#6b7b86`) is for secondary text — timestamps, metadata labels,
captions, breadcrumbs, placeholder text, and anything that supports the primary
content without competing for attention. The same value works in both themes
because it sits at the midpoint of the contrast range.

**Accent** (`#0d9488` / `#14b8a6`) is for text that needs to signal
interactivity or importance — links, code keywords in syntax highlighting,
status labels, and the occasional emphasized term in prose. Use sparingly in
running text. If more than ~10% of visible text is accent-colored, the emphasis
loses its meaning.

### Backgrounds

**Background** (`#f6f7f8` / `#111416`) is the page-level canvas. No gradients,
no textures — a flat, quiet surface that lets the content and the accent color
do the work.

**Surface** (`#e9ecef` / `#1a1e21`) is for any element that sits above the
background: cards, panels, code blocks, modals, dropdown menus, the sidebar if
there is one. The contrast between background and surface should be subtle —
just enough to create a sense of elevation without a visible border.

**Accent Subtle** (`rgba(13,148,136,0.08)` / `rgba(20,184,166,0.08)`) is for
tinted backgrounds behind accent-related content: a callout box about a feature,
a tag indicating "evaluation passed," a highlighted row in a table. The 8%
opacity creates a whisper of teal without making the background compete with the
text.

### Borders & Dividers

**Border** (`#d5d9dd` / `#262c30`) is the only border color. Use it for card
edges, horizontal rules, table cell borders, and input field outlines. Do not
mix border weights — use 1px for all borders. If a border needs more emphasis,
use the structure color at reduced opacity rather than a thicker line.

### Interactive Elements

Links and buttons use the **Accent** color as their resting state. Hover states
darken to **Accent Hover** (`#0b7e74` /
`#1cc9b4`). Active/pressed states can darken one more step or use the accent at
90% opacity. Focus rings should use the accent color at 50% opacity as a 2px
outline offset.

Disabled elements use the **Muted** color at 50% opacity for both text and
borders.

### Status & Semantic Colors

**Success** (`#1a7a5a` / `#2dcc8a`) — passed evaluations, successful CI runs,
healthy system status. In DecisionLedger's context: an ALLOW decision, a passing
eval gate.

**Danger** (`#c0392b` / `#e05545`) — failed evaluations, errors, system
failures. A BLOCK decision, a failing eval gate, an error in replay.

**Warning** (`#b8860b` / `#daa520`) — CHALLENGE decisions, degraded performance,
approaching thresholds, drift detected.

**Accent** (teal) — neutral-positive system activity, informational states,
active processes, links to decision records.

### The Logo

The DecisionLedger logo uses only **Structure** and **Accent** colors. In the
full mark, the converging and diverging structural strokes use the Structure
color. The decision bar and the four fading record bars use the Accent color.
The record bars use the accent at decreasing opacities: 0.6, 0.5, 0.4, 0.3.

The favicon uses the same two colors at heavier stroke weights with wider
angles. No record bars.

Never place the logo on a background color that reduces the contrast of either
the structure strokes or the accent bar below a 3:1 ratio. On the light theme,
this means avoiding backgrounds darker than approximately `#c0c5ca`. On the dark
theme, avoid backgrounds lighter than approximately `#3a4048`.

### Code & Syntax Highlighting

For code blocks on the **Surface** background, use the following mapping:

| Syntax element | Color                 | Notes                                             |
|----------------|-----------------------|---------------------------------------------------|
| Default text   | Structure             |                                                   |
| Keywords       | Accent                | `import`, `return`, `if`, `class`                 |
| Strings        | `#b8860b` / `#daa520` | The warning/gold tone — warm contrast to the teal |
| Comments       | Muted                 |                                                   |
| Functions      | Structure             | Same as default but can be bold                   |
| Numbers        | `#1a7a5a` / `#2dcc8a` | The success/green tone                            |
| Operators      | Muted                 |                                                   |

---

## Typography Pairing Recommendation

The palette was designed with these typeface pairings in mind, though they are
recommendations rather than strict requirements:

**Headings:** A clean sans-serif with character — Outfit, General Sans, or
Satoshi. Avoid Inter (overused in dev tooling), Roboto (generic), or Space
Grotesk (AI-project cliché).

**Body text:** A readable sans-serif or serif — the palette's warmth works with
either. For a more editorial/considered feel, pair with a serif like Newsreader
or Source Serif. For a cleaner engineering feel, use the heading font at regular
weight.

**Monospace (code, labels, metadata):** JetBrains Mono, Berkeley Mono, or DM
Mono. The palette's muted color works especially well with monospace text for
secondary labels and metadata.

---

## Accessibility Notes

All primary text combinations meet WCAG 2.1 AA contrast requirements:

| Combination                     | Contrast Ratio | Rating          |
|---------------------------------|----------------|-----------------|
| Structure on Background (light) | ~14.5:1        | AAA             |
| Structure on Background (dark)  | ~14.2:1        | AAA             |
| Accent on Background (light)    | ~4.6:1         | AA              |
| Accent on Background (dark)     | ~5.8:1         | AA              |
| Muted on Background (light)     | ~4.5:1         | AA              |
| Muted on Background (dark)      | ~3.8:1         | AA (large text) |

The muted color on dark backgrounds is the tightest ratio. For small text below
14px in the dark theme, consider using the Structure color at reduced opacity
rather than the Muted color if the text needs to be readable rather than purely
decorative.
