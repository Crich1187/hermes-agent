# Hermes `/insights` output format (root-e4k)

Local analytics over session history. Output is **aggregate only** — no prompts,
account identifiers, raw billing records, tokens, or credentials.

## Invocation

```bash
# Slash command (CLI / gateway)
/insights 30

# Equivalent CLI
hermes insights --days 30
hermes insights --days 14 --source telegram
```

`--days N` selects sessions with `started_at >= now - N*86400`.
`--source` optionally filters by platform (`cli`, `telegram`, …).

## Sections (terminal)

| Section | Contents |
|---|---|
| Overview | Session / message / tool / token counts; active time |
| Cost | Estimated USD (when priced), **Included** subscription sessions, **Unknown** (no price table) |
| **Providers** | Canonical provider rows (see below) |
| Models | Per-model session + token totals |
| Platforms | Per-source session / message / token totals |
| Top Tools / Skills | Call counts (tool names / skill names only) |
| Activity | Day-of-week and peak-hour histograms |
| Notable Sessions | Truncated session id prefixes + aggregate metrics |

## Provider attribution

Required providers always appear: **`nous`**, **`zai`**, **`alibaba`**, **`anthropic`**.

Canonicalization (examples):

| Stored / inferred | Canonical key |
|---|---|
| `zai`, z.ai / bigmodel hosts | `zai` |
| `alibaba`, `alibaba-coding-plan`, dashscope hosts | `alibaba` |
| `anthropic`, api.anthropic.com | `anthropic` |
| `nous`, nousresearch inference hosts | `nous` |
| empty / unrecognized | `unknown` (other row; not a required key) |

**Status labels (distinguish missing vs zero):**

| Status | Meaning |
|---|---|
| `no data` / `missing` | No sessions attributed to that provider in the window |
| `zero spend` | Sessions exist; invoiceable estimate is $0 (e.g. subscription included) |
| `unknown $` / `unknown_pricing` | Sessions exist; no pricing data for those models |
| `$X.XX` / `spend` | Sessions exist with a positive estimated cost |

Example (redacted aggregates):

```
  🏷️  Providers
  ────────────────────────────────────────────────────────
  Provider       Sessions       Tokens           Status
  nous                  0            0          no data
  zai                 168   72,975,499       unknown $
  alibaba              87   19,420,327       unknown $
  anthropic             4    9,225,427           ~$0.00
```

## Surfaces

| Surface | Formatter | Notes |
|---|---|---|
| `hermes insights` CLI | `InsightsEngine.format_terminal` | Full table layout |
| `/insights` slash command | `InsightsEngine.format_gateway` | Compact markdown; **AC surface** |

Both formatters emit the required Providers block with the same status vocabulary.

## Privacy / safety

- Do not paste raw `/insights` output into tickets if it includes unredacted session
  id prefixes you consider sensitive; truncate further if needed.
- Never pair insights output with env dumps, `auth.json`, or provider invoices.
- Analytics are read-only against the local Hermes state database.
