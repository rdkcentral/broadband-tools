# emit_hal_spec_json.py — Southbound HAL API Extractor

Companion to your northbound `emit_spec_json.py` (TR-181). This walks a
directory of `rdkb-halif-<component>` checkouts and emits one JSON file
per component, listing every HAL C-API declared in that component's
header(s) — name, return type, params, doc (brief/@param/@return),
deprecated flag, and source line.

Pure stdlib, no pip installs, no libclang — safe to drop on any build
host and run manually.

## Usage

**For your setup** (a folder containing all the `rdkb-halif-*` checkouts as
siblings, e.g. your `sb` directory) — run it from inside that folder and it
writes each component's JSON straight into that component's own repo folder:

```bash
cd sb
python3 emit_hal_spec_json.py --root . --in-place --summary
```

This produces:

```
sb/
├── rdkb-halif-cm/
│   └── cm.json          <- written directly inside the repo
├── rdkb-halif-wifi/
│   └── wifi.json
├── rdkb-halif-mta/
│   └── mta.json
... (one .json per repo, inside that repo)
├── _summary.json         <- cross-component counts (from --summary)
└── _all_apis.json        <- every API from every component, flattened
```

`_summary.json`/`_all_apis.json` span all components so they can't live
inside a single repo folder — they go to `--out` if you pass one, otherwise
the current directory.

**Alternative — separate output tree** (doesn't touch your repo checkouts
at all, everything lands under one folder you point at):

```bash
python3 emit_hal_spec_json.py --root /path/to/halif/checkouts --out ./hal_spec_json --summary
```

```
hal_spec_json/
├── cm/cm.json
├── wifi/wifi.json
├── ...
├── _summary.json
└── _all_apis.json
```

**Single header, quick check:**

```bash
python3 emit_hal_spec_json.py --header /path/to/cm_hal.h --out ./out
```

`--root` expects a directory containing sibling repo checkouts, e.g.:

```
halif/
  rdkb-halif-cm/
    include/cm_hal.h
  rdkb-halif-mta/
    include/mta_hal.h
  rdkb-halif-wifi/
    include/wifi_hal_ap.h
    include/wifi_hal_radio.h
    ...
```

It also falls back to a bare `*_hal*.h` at the repo root (covers repos
like `rdkb-halif-epon` that don't use an `include/` subfolder).

## Output

- `--in-place`: `<repo>/<component>.json` written directly inside each
  component's own checkout folder — see above.
- Otherwise (`--out` without `--in-place`): `<out>/<component>/<component>.json`
  — one subfolder per component, all under a single tree you point at.
- `_summary.json` — component/header/API counts (with `--summary`).
- `_all_apis.json` — every API from every component flattened into one
  array, each entry tagged with `component` and `header` (with `--summary`).

## Run against your uploaded archive

I ran it against `rdkb-halif_tar.gz` as a smoke test:

| component | APIs | component | APIs |
|---|---|---|---|
| wifi | 691 | ethsw | 20 |
| platform | 71 | vlan | 16 |
| dhcp | 54 | wan | 15 |
| cm | 51 | epon | 15 |
| mta | 49 | lpa | 11 |
| cellular | 36 | fwupgrade | 10 |
| moca | 21 | mso, cellular-modem, bridge-util | 3 |
| dhcp6c, emmc | 2 | gpon, voice, xdsl | 0 (no header in this checkout) |

**Total: 1,073 HAL APIs across 21 components.**

`gpon`, `voice`, and `xdsl` came back at 0 because those checkouts in
your archive only contain LICENSE/CODEOWNERS — no `include/*.h` — so
there's nothing to extract yet, not a parser miss.

`hal_spec_json/` in this delivery is that actual run's output, so you
can inspect real examples before wiring it into anything.

## How it parses (so you can trust/extend it)

RDK-B HAL headers are hand-written to a very consistent house style —
one prototype per statement, `/** ... */` doxygen directly above it —
so this avoids pulling in a real C front end. It:

1. Strips comments (keeping line numbers intact, and separately
   keeping each `/** */` block indexed by its end line, for docs).
2. Strips the standard `#ifdef __cplusplus / extern "C"{ / #endif`
   wrapper (otherwise it makes every real declaration look like it's
   nested one brace deep).
3. Walks the remaining text with a paren/brace-depth-aware statement
   splitter — splits on `;` only at depth 0, so a `struct {...};` or a
   prototype whose params happen to wrap onto more than one physical
   line is still captured as one statement.
4. Matches each statement against a function-prototype pattern, and
   explicitly skips `typedef` (including function-pointer typedefs),
   `struct/enum/union` bodies, and `extern`/`static`.
5. Attaches the nearest preceding doxygen block as the function's docs.

Sanity-checked against a naive single-line grep across the archive:
every place the two disagreed, the grep either mis-fired on a function-
pointer `typedef` (which this tool correctly excludes) or missed a
prototype whose parameter list wraps across lines (which this tool
correctly includes) — e.g. `wifi_hal_deprecated.h` has real multi-line
prototypes that inflate its count past what single-line grep sees.

### Known non-goals / things to watch
- Two components with the *same function name* declared under
  different `#ifdef` variants (seen a couple of times in
  `wifi_hal_ap.h`/`wifi_hal_deprecated.h`) will show up as two entries
  with different signatures — the tool doesn't evaluate preprocessor
  conditionals, so it records everything textually present.
- `#define`d constants/macros are not extracted — this is a *function*
  (API) extractor. Say the word if you also want macros/enums/structs
  captured as a `data_types` section; the statement splitter already
  isolates them, it's a small add.
