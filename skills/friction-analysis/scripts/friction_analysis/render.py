"""Render the friction report: one self-contained HTML file, picture-first.

Inline CSS and inline SVG only: no scripts, fonts, images or network. CSS-only
interactivity (radio tabs, :hover/:focus inspectors, <details>). Dark by default
with a light toggle. Each color carries exactly one meaning and always travels
with a text label:

    red    the CLI's own defect      blue   a missing mapping or flag
    yellow feature-gated             aqua   works on the latest binary
    gray   noise / undecided

That four-hue order was validated adjacent-pair for CVD and normal vision in
both modes. The page only ever prints anonymized shapes and counts; a final leak
check refuses to write a report that contains a home path, email, UUID or secret.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from .classify import BUCKETS, FAMILY
from .normalize import PRIVACY_DETECTORS
from .util import plural

FAMILY_ORDER = ('defect', 'gap', 'gated', 'works', 'noise')
BUCKET_LABEL = {
    'cli-taught': 'CLI taught it',
    'word-split': 'word split',
    'catalog-miss': 'catalog miss',
    'bad-fuzzy': 'bad fuzzy match',
    'alias': 'no alias yet',
    'flag-drift': 'flag drift',
    'missing-feature': 'missing feature',
    'gated': 'gated off',
    'fixed': 'works now',
    'noise': 'probes',
    'unclassified': 'undecided',
}
BUCKET_RULE = {
    'cli-taught': "the CLI's own hints, help or docs name it",
    'word-split': 'a multi-word command path arrived as one argument',
    'catalog-miss': 'a catalog entry exists but misses, or targets nothing',
    'bad-fuzzy': 'fuzzy matching suggests an unrelated command',
    'alias': 'a desire path with a clear target',
    'flag-drift': 'a flag the command lacks, often borrowed from a sibling',
    'missing-feature': 'agents want something that does not exist',
    'gated': 'a feature-gated command invoked while disabled',
    'fixed': 'works on the current binary',
    'noise': 'deliberate probes and non-commands',
    'unclassified': 'still needs a decision',
}
PRIVACY_SHORT = {
    'home-path': 'home paths',
    'absolute-path': 'file paths',
    'email': 'emails',
    'url': 'URLs',
    'id': 'IDs',
    'secret-like': 'secret-like tokens',
    'free-text': 'free text',
    'multi-line': 'pasted text',
    'near-limit': 'long inputs',
}
COLUMN_LABEL = {'input': 'input', 'error': 'error message'}
LEAK_CHECKS = ('home-path', 'email', 'secret-like')
UUID = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.IGNORECASE)


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def code(value: Any) -> str:
    return f'<code>{esc(value)}</code>'


def family_label(family: str, cli: str) -> str:
    return {'defect': f"{cli}'s own bugs", 'gap': 'map gaps', 'gated': 'gated', 'works': 'works now', 'noise': 'noise'}[
        family
    ]


class LeakError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# css


CSS = """
:root{color-scheme:dark;
  --bg:#0d0f0d;--panel:#141613;--panel2:#1a1d19;--hair:#262a25;--hair2:#1f231e;
  --ink:#f0efec;--dim:#c3c2b7;--faint:#9a998f;--onfill:#0b0b0b;
  --defect:#e66767;--gap:#3987e5;--gated:#c98500;--works:#199e70;--noise:#77786f;
  --seq1:#1c5cab;--seq2:#3987e5;--seq3:#86b6ef;--seq1ink:#f0efec}
:root:has(#theme-light:checked){color-scheme:light;
  --bg:#f9f9f7;--panel:#fcfcfb;--panel2:#f2f1ed;--hair:#e1e0d9;--hair2:#ebeae5;
  --ink:#0b0b0b;--dim:#52514e;--faint:#66655f;--onfill:#0b0b0b;
  --defect:#e34948;--gap:#2a78d6;--gated:#eda100;--works:#1baf7a;--noise:#a3a29b;
  --seq1:#b7d3f6;--seq2:#6da7ec;--seq3:#2a78d6;--seq1ink:#0b0b0b}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink)}
body{font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif;padding:0 0 56px}
h1,h2,h3{font-weight:600;letter-spacing:-.01em;margin:0}
h1{font-size:30px;line-height:1.2}
h3{font-size:16px}
code,pre,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.86em}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.wrap{max-width:1080px;margin:0 auto;padding:0 28px}
.eyebrow{font:11px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.12em;text-transform:uppercase;color:var(--faint)}
header.top{padding:32px 0 16px;border-bottom:1px solid var(--hair)}
header.top p{color:var(--dim);margin:8px 0 0;max-width:780px}
.theme{float:right;font-size:12px;color:var(--faint);cursor:pointer}
.theme input{accent-color:var(--gap);vertical-align:middle}
.tabs{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--hair);padding:10px 0}
.tabs input{position:absolute;opacity:0;pointer-events:none}
.tabs label{display:inline-block;padding:6px 12px;margin-right:4px;border-radius:999px;color:var(--dim);cursor:pointer;font-size:13px;border:1px solid transparent}
.tabs label:hover{color:var(--ink);border-color:var(--hair)}
.tabs input:focus-visible+label{outline:2px solid var(--gap);outline-offset:2px}
.view{padding:24px 0 8px}
.view:not(#v1){display:none}
body:has(.tabs input:not(#t1):checked) #v1{display:none}
body:has(#t2:checked) #v2,body:has(#t3:checked) #v3,body:has(#t4:checked) #v4,body:has(#t5:checked) #v5{display:block}
body:has(#t1:checked) label[for=t1],body:has(#t2:checked) label[for=t2],body:has(#t3:checked) label[for=t3],
body:has(#t4:checked) label[for=t4],body:has(#t5:checked) label[for=t5]{color:var(--ink);background:var(--panel2);border-color:var(--hair)}
.lede{font-size:20px;line-height:1.35;margin:0 0 16px;max-width:820px}
.cap{color:var(--faint);font-size:12.5px;margin:8px 0 0}
.card{background:var(--panel);border:1px solid var(--hair);border-radius:12px;padding:14px 16px}
.card p{margin:4px 0 0;color:var(--dim);font-size:14px}
.k{font:11px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.1em;text-transform:uppercase;color:var(--faint);display:flex;align-items:center;gap:6px}
.sw{display:inline-block;width:10px;height:10px;border-radius:3px;flex:none}
.sw.still-key{background:var(--ink);opacity:.62;height:4px;width:14px;border-radius:2px}
.sw.defect{background:var(--defect)}.sw.gap{background:var(--gap)}.sw.gated{background:var(--gated)}.sw.works{background:var(--works)}.sw.noise{background:var(--noise)}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:16px}
.action{border-top:3px solid var(--hair)}
.action.defect{border-top-color:var(--defect)}.action.gap{border-top-color:var(--gap)}.action.gated{border-top-color:var(--gated)}
.action h3{margin-top:6px}
.n{font-variant-numeric:tabular-nums}
.tile .big{font-size:30px;font-weight:600;line-height:1.1;margin-top:4px}
.tile .big small{font-size:14px;font-weight:400;color:var(--dim)}
.legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--dim);font-size:12.5px;margin:10px 0 0;align-items:center}
.legend span{display:inline-flex;align-items:center;gap:6px}
svg{display:block;width:100%;height:auto}
svg text{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;fill:var(--ink)}
svg .onfill{fill:var(--onfill)} svg .dim{fill:var(--dim)} svg .faint{fill:var(--faint)}
.seg.defect{fill:var(--defect)}.seg.gap{fill:var(--gap)}.seg.gated{fill:var(--gated)}.seg.works{fill:var(--works)}.seg.noise{fill:var(--noise)}
.seg.alt{opacity:.74}
.still{fill:var(--ink);opacity:.62}
.chips{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0}
.chip{border:1px solid var(--hair);background:var(--panel);border-radius:10px;padding:7px 12px;cursor:default;font-size:13px;color:var(--dim)}
.chip b{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;margin-right:6px;color:var(--ink)}
.chip:hover,.chip:focus{border-color:var(--ink);color:var(--ink);outline:none}
.dock{min-height:110px}
.dock .e{display:none}.dock .e0{display:block}
.dock .fix{color:var(--ink);margin-top:8px}
.marker circle{fill:var(--panel);stroke:var(--defect);stroke-width:2;transition:r .15s ease}
.marker.gated circle{stroke:var(--gated)}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th{text-align:left;font-weight:500;color:var(--faint);font-size:11px;letter-spacing:.08em;text-transform:uppercase;padding:6px 10px;border-bottom:1px solid var(--hair)}
td{padding:7px 10px;border-bottom:1px solid var(--hair2);vertical-align:top}
td.n,th.n{text-align:right}
td.n{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--dim)}
.mode{font:600 11px/1 ui-monospace,SFMono-Regular,Menlo,monospace;padding:3px 8px;border-radius:999px;border:1px solid;white-space:nowrap;display:inline-block}
.mode.auto{background:var(--gap);border-color:var(--gap);color:var(--onfill)}
.mode.ask{color:var(--ink);border-color:var(--gap)}
.mode.honest{color:var(--ink);border-color:var(--gated)}
.mode.clean,.mode.fixm{color:var(--ink);border-color:var(--defect)}
.mx th,.mx td{text-align:center}
.mx th{text-transform:none;letter-spacing:0;font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--dim)}
.mx td:first-child,.mx th:first-child{text-align:left}
.has{color:var(--dim)}
.miss{display:inline-block;min-width:30px;border-radius:6px;font:600 12px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;padding:0 7px;color:var(--onfill)}
.miss.s1{background:var(--seq1);color:var(--seq1ink)}.miss.s2{background:var(--seq2)}.miss.s3{background:var(--seq3)}
.add{display:inline-block;border:1px dashed var(--gap);border-radius:6px;font-size:11px;padding:0 7px;color:var(--ink)}
.na{color:var(--faint)}
.bars{display:grid;grid-template-columns:minmax(160px,240px) 1fr 52px;gap:8px 14px;align-items:center;font-size:13.5px}
.bars .track{background:var(--panel2);border-radius:4px;height:12px;overflow:hidden}
.bars .fill{display:block;height:12px;background:var(--defect);border-radius:0 4px 4px 0}
.bars .v{text-align:right;color:var(--dim)}
.ok{display:inline-flex;align-items:center;gap:8px}
.ok .sw{background:var(--works)}
details.notes{margin-top:36px;border:1px solid var(--hair);border-radius:12px;background:var(--panel)}
details.notes summary{cursor:pointer;padding:14px 18px;font-size:17px;font-weight:600}
details.notes .body{padding:0 22px 22px;color:var(--dim);font-size:14px}
details.notes h3{color:var(--ink);margin:22px 0 6px}
details.notes li{margin:3px 0}
details.notes code{color:var(--ink)}
details.notes pre{background:var(--panel2);border:1px solid var(--hair);border-radius:8px;padding:12px;overflow:auto;color:var(--ink);max-height:420px}
footer{color:var(--faint);font-size:12px;margin-top:28px}
@media (max-width:820px){.grid3{grid-template-columns:1fr}.bars{grid-template-columns:1fr 1fr 44px}}
"""


# ---------------------------------------------------------------------------
# views


def hero_bar(buckets: dict[str, dict[str, int]], total: int, cli: str) -> str:
    width, gap = 1000.0, 2.0
    present = [b for b in BUCKETS if b in buckets and buckets[b]['events']]
    usable = width - gap * (len(present) - 1)
    x = 0.0
    parts: list[str] = []
    below: list[tuple[float, str]] = []  # narrow segments get their label under the bar
    previous_family = ''
    alternate = False
    for bucket in present:
        events = buckets[bucket]['events']
        still = buckets[bucket]['still_fails']
        segment = usable * events / total if total else 0
        family = FAMILY[bucket]
        alternate = (not alternate) if family == previous_family else False
        previous_family = family
        title = f'{BUCKET_LABEL[bucket]}: {plural(events, "event")}, {still} still fail on the latest binary'
        parts.append(
            f'<g><title>{esc(title)}</title>'
            f'<rect class="seg {family}{" alt" if alternate else ""}" x="{x:.1f}" y="4" width="{max(segment, 1):.1f}" height="60" rx="4"/>'
        )
        if still:
            parts.append(
                f'<rect class="still" x="{x:.1f}" y="70" width="{max(usable * still / total, 1):.1f}" height="8" rx="2"/>'
            )
        label = BUCKET_LABEL[bucket]
        if segment >= max(len(label) * 6.7, len(str(events)) * 9) + 18:
            parts.append(
                f'<text class="onfill" x="{x + 9:.1f}" y="30" font-size="15" font-weight="650">{events}</text>'
                f'<text class="onfill" x="{x + 9:.1f}" y="49" font-size="12">{esc(label)}</text>'
            )
        elif segment >= len(str(events)) * 9 + 12:
            parts.append(
                f'<text class="onfill" x="{x + 6:.1f}" y="30" font-size="14" font-weight="650">{events}</text>'
            )
            below.append((x, label))
        else:
            below.append((x, f'{events} {label}'))
        parts.append('</g>')
        x += segment + gap
    rows = place_labels(below, width)
    placed: list[str] = []
    for (start, label), (row, left) in zip(below, rows):
        y = 98 + row * 17
        placed.append(
            f'<line x1="{start + 1.5:.1f}" y1="80" x2="{start + 1.5:.1f}" y2="{y - 10}" style="stroke:var(--faint)" stroke-width="1"/>'
            f'<text class="dim" x="{left:.1f}" y="{y}" font-size="11.5">{esc(label)}</text>'
        )
    row_count = max((row for row, _ in rows), default=-1) + 1
    height = 84 + 17 * row_count + (6 if row_count else 0)
    return (
        f'<svg viewBox="0 0 1000 {height}" role="img" aria-label="{esc(f"{total} friction events by root cause")}">'
        + ''.join(parts)
        + ''.join(placed)
        + '</svg>'
    )


def place_labels(labels: list[tuple[float, str]], width: float) -> list[tuple[int, float]]:
    """Stack labels under narrow segments so no label overlaps another and no leader line crosses one.

    Placed right to left: a leader then only ever passes rows whose labels sit to its right.
    """
    spans: list[list[tuple[float, float]]] = []
    result: dict[int, tuple[int, float]] = {}
    for index in sorted(range(len(labels)), key=lambda i: -labels[i][0]):
        start, label = labels[index]
        text_width = len(label) * 6.4 + 4
        left = min(start + 4, width - text_width)
        leader = start + 1.5
        row = 0
        while True:
            if row == len(spans):
                spans.append([])
            free = all(left + text_width + 8 <= a or left >= b + 8 for a, b in spans[row])
            uncrossed = all(not (a - 3 <= leader <= b + 3) for upper in spans[:row] for a, b in upper)
            if free and uncrossed:
                break
            row += 1
        spans[row].append((left, left + text_width))
        result[index] = (row, left)
    return [result[i] for i in range(len(labels))]


def family_legend(buckets: dict[str, dict[str, int]], cli: str) -> str:
    totals = {family: 0 for family in FAMILY_ORDER}
    for bucket, values in buckets.items():
        totals[FAMILY[bucket]] += values['events']
    items = [
        f'<span><i class="sw {family}"></i>{esc(family_label(family, cli))} <b class="n">{totals[family]}</b></span>'
        for family in FAMILY_ORDER
        if totals[family]
    ]
    still = sum(values['still_fails'] for values in buckets.values())
    items.append(f'<span><i class="sw still-key"></i>still fails <b class="n">{still}</b></span>')
    return '<div class="legend">' + ''.join(items) + '</div>'


def summary_view(data: dict[str, Any]) -> str:
    cli, profile, rec = data['cli'], data['profile'], data['rec']
    buckets = rec['buckets']
    total = profile['events'] or 1
    still = sum(b['still_fails'] for b in buckets.values())
    fixable = sum(b['events'] for name, b in buckets.items() if FAMILY[name] in ('defect', 'gap', 'gated'))
    agents = profile['actors'].get('agent', 0)
    estimate = rec['estimate']
    tiles = (
        f'<div class="card tile"><div class="k">Still fails on latest</div><div class="big n">{still} <small>of {total}</small></div></div>'
        f'<div class="card tile"><div class="k">Works after fixes</div><div class="big n">≈{estimate["works_after"]} <small>of {total}</small></div></div>'
        f'<div class="card tile"><div class="k">From AI coworkers</div><div class="big n">{round(100 * agents / total)}%</div></div>'
    )
    actions = ''.join(
        f'<div class="card action {esc(a["family"])}"><div class="k"><i class="sw {esc(a["family"])}"></i>'
        f'{"privacy" if a["id"] == "privacy" else esc(BUCKET_LABEL.get(a["id"], a["id"]))}</div>'
        f'<h3>{esc(a["title"])}</h3><p>{esc(a["detail"])}</p></div>'
        for a in rec['top_actions']
    )
    lede = f'{round(100 * fixable / total)}% have a concrete fix in {cli} or its catalog, not in the agents.'
    return (
        f'<section class="view" id="v1"><h2 class="sr">Summary</h2><p class="lede">{esc(lede)}</p>'
        f'{hero_bar(buckets, total, cli)}{family_legend(buckets, cli)}'
        f'<div class="grid3">{tiles}</div><div class="grid3">{actions}</div></section>'
    )


LEAK_STAGES = ('CLI guidance', 'agent + shell', 'parser', 'catalog lookup', 'fuzzy match', 'right command')
# x of the leak marker: on the arrow for hand-offs, on the box for a stage that drops the input
LEAK_POSITION = {'cli-taught': 160, 'word-split': 330, 'gated': 415, 'catalog-miss': 585, 'bad-fuzzy': 755}


def leak_entries(data: dict[str, Any]) -> list[dict[str, str]]:
    cli, rec, profile = data['cli'], data['rec'], data['profile']
    buckets = rec['buckets']
    shapes = rec['shapes']
    first = {bucket: next((s for s in shapes if s['bucket'] == bucket), None) for bucket in LEAK_POSITION}
    entries = []
    if 'cli-taught' in buckets:
        strings = rec['strings_to_fix']
        example = strings[0]['ref'] if strings else ' '.join([cli] + (first['cli-taught'] or {}).get('argv', [])[:3])
        entries.append(
            {
                'bucket': 'cli-taught',
                'title': f'{cli} names commands that fail',
                'body': f"{plural(len(strings), 'reference')} in {cli}'s own strings or help fail, e.g. {code(example)}.",
                'fix': 'Correct the strings; test that every reference resolves.',
            }
        )
    if 'word-split' in buckets:
        shape = first['word-split'] or {}
        words = ' '.join(
            shape.get('argv', [])[shape.get('joined_at', 0) : shape.get('joined_at', 0) + shape.get('joined', 0)]
        )
        events = buckets['word-split']['events']
        burst = profile.get('word_split_burst_events', 0)
        entries.append(
            {
                'bucket': 'word-split',
                'title': 'The shell passed a whole path as one argument',
                'body': f'{code(chr(34) + words + chr(34))} arrived as one argv. Agents loop over command names and '
                f'zsh does not split {code("$var")}. {burst} of {events} came in sub-second bursts.',
                'fix': 'If the joined argument resolves, split it and run.',
            }
        )
    if 'gated' in buckets:
        names = [row['command'] for row in rec['gated'][:3]]
        entries.append(
            {
                'bucket': 'gated',
                'title': 'Gated commands look unknown',
                'body': f'{", ".join(code(n) for n in names)} exist behind a feature flag. Agents learned the names somewhere.',
                'fix': 'Say "not enabled for this account"; find the leak.',
            }
        )
    if 'catalog-miss' in buckets:
        shape = first['catalog-miss'] or {}
        entries.append(
            {
                'bucket': 'catalog-miss',
                'title': 'Catalog entries miss on trailing arguments',
                'body': f'{code(" ".join(shape.get("argv", [])))}: {esc(shape.get("reason", ""))}.',
                'fix': 'Match the longest prefix and carry the rest; validate targets.',
            }
        )
    if 'bad-fuzzy' in buckets:
        rows = rec['fuzzy'][:2] or [
            {
                'typed': (first['bad-fuzzy'] or {}).get('unknown_word', ''),
                'suggested': (first['bad-fuzzy'] or {}).get('fuzzy', {}).get('suggested', ''),
            }
        ]
        pairs = ', '.join(f'{code(r["typed"])} → {code(r["suggested"])}' for r in rows)
        entries.append(
            {
                'bucket': 'bad-fuzzy',
                'title': 'Fuzzy matching picks unrelated commands',
                'body': f'{pairs}. Short words sit within two edits of everything.',
                'fix': 'Scale distance to word length; never fuzzy-suggest a mutating command.',
            }
        )
    return entries


def leaks_view(data: dict[str, Any]) -> str:
    rec = data['rec']
    buckets = rec['buckets']
    entries = leak_entries(data)
    letters = 'ABCDEF'
    through = sum(buckets.get(b, {}).get('events', 0) for b in ('alias', 'flag-drift', 'missing-feature'))
    svg = [
        '<svg viewBox="0 0 1000 150" role="img" aria-label="Invocation pipeline with leak points">',
        '<defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">'
        '<path d="M0,0L10,5L0,10z" style="fill:var(--faint)"/></marker></defs>',
    ]
    for index, stage in enumerate(LEAK_STAGES):
        x = index * 170
        final = index == len(LEAK_STAGES) - 1
        stroke = 'var(--works)' if final else 'var(--hair)'
        svg.append(
            f'<rect x="{x}" y="58" width="150" height="46" rx="8" style="fill:var(--panel2);stroke:{stroke}"/>'
            f'<text x="{x + 75}" y="86" text-anchor="middle" font-size="13">{esc(stage)}</text>'
        )
        if not final:
            svg.append(
                f'<line x1="{x + 152}" y1="81" x2="{x + 166}" y2="81" style="stroke:var(--faint)" stroke-width="1.5" marker-end="url(#ar)"/>'
            )
    for index, entry in enumerate(entries):
        cx = LEAK_POSITION[entry['bucket']]
        gated = ' gated' if entry['bucket'] == 'gated' else ''
        svg.append(
            f'<g class="marker m{index}{gated}"><circle cx="{cx}" cy="34" r="13"/>'
            f'<text x="{cx}" y="39" text-anchor="middle" font-size="13" font-weight="650">{letters[index]}</text></g>'
        )
    svg.append(
        f'<text class="faint" x="0" y="136" font-size="12.5">{through} events fall through every stage: '
        'new entries and flags fix those, not the engine.</text></svg>'
    )
    chips = ''.join(
        f'<span class="chip c{index}" tabindex="0"><b>{letters[index]}</b>'
        f'{esc(BUCKET_LABEL[e["bucket"]])} · <span class="n">{buckets[e["bucket"]]["events"]}</span></span>'
        for index, e in enumerate(entries)
    )
    dock = ''.join(
        f'<div class="e e{index}"><h3>{esc(e["title"])}</h3><p>{e["body"]}</p><p class="fix">Fix: {esc(e["fix"])}</p></div>'
        for index, e in enumerate(entries)
    )
    if not entries:
        dock = '<div class="e e0"><p class="ok"><i class="sw"></i>No engine or CLI defects in this export.</p></div>'
    lede = f'{plural(len(entries), "leak")} between what an agent types and a working command.' + (
        ' Hover one.' if entries else ''
    )
    return (
        f'<section class="view" id="v2"><h2 class="sr">Where it leaks</h2><p class="lede">{esc(lede)}</p>'
        f'{"".join(svg)}<div class="chips">{chips}</div><div class="card dock">{dock}</div></section>'
    )


def leak_css(count: int) -> str:
    rules = []
    for index in range(1, count):
        rules.append(
            f'#v2:has(.c{index}:hover,.c{index}:focus) .dock .e0{{display:none}}'
            f'#v2:has(.c{index}:hover,.c{index}:focus) .dock .e{index}{{display:block}}'
        )
    for index in range(count):
        rules.append(f'#v2:has(.c{index}:hover,.c{index}:focus) .m{index} circle{{r:16px;fill:var(--panel2)}}')
    return ''.join(rules)


def map_rows(rec: dict[str, Any], cli: str) -> list[dict[str, Any]]:
    rows = [
        {'typed': e['typed'], 'result': e['target'], 'mode': e['mode'], 'events': e['events']}
        for e in rec['catalog_entries']
    ]
    for row in rec['gated']:
        rows.append(
            {
                'typed': row['command'],
                'result': '"not enabled for this account"',
                'mode': 'HONEST',
                'events': row['events'],
                'plain': True,
            }
        )
    for row in rec['fuzzy']:
        rows.append(
            {
                'typed': row['typed'],
                'result': f'no suggestion (today {row["suggested"]})',
                'mode': 'FIX',
                'events': row['events'],
                'plain': True,
            }
        )
    for row in rec['catalog_cleanup']:
        if row.get('replaced_by_entry'):
            continue
        result = f'retarget → {row["target"]}' if row.get('target') else row['action']
        events = sum(
            s['count']
            for s in rec['shapes']
            if s['bucket'] == 'catalog-miss' and s['argv'][: len(row['pattern'].split())] == row['pattern'].split()
        )
        rows.append(
            {'typed': row['pattern'], 'result': result, 'mode': 'CLEAN', 'events': events or None, 'plain': True}
        )
    return sorted(rows, key=lambda r: (-(r['events'] or 0), r['typed']))


def map_view(data: dict[str, Any]) -> str:
    rec, cli = data['rec'], data['cli']
    rows = map_rows(rec, cli)
    shown, hidden = rows[:7], len(rows) - 7
    body = ''.join(
        f'<tr><td>{code(r["typed"])}</td><td>{esc(r["result"]) if r.get("plain") else code(r["result"])}</td>'
        f'<td><span class="mode {"fixm" if r["mode"] == "FIX" else r["mode"].lower()}">{r["mode"]}</span></td>'
        f'<td class="n">{r["events"] if r["events"] else "—"}</td></tr>'
        for r in shown
    )
    auto = sum(1 for e in rec['catalog_entries'] if e['mode'] == 'AUTO')
    lede = (
        (
            f'{plural(len(rec["catalog_entries"]), "new entry", "new entries")}, {auto} AUTO. '
            'AUTO only when the verb is preserved.'
        )
        if rows
        else 'No map changes needed.'
    )
    more = f'<p class="cap">+{hidden} more in Implementation notes.</p>' if hidden > 0 else ''
    table = (
        (
            f'<table><thead><tr><th>Agent typed</th><th>{esc(cli)} runs or says</th><th>Mode</th><th class="n">Events</th></tr></thead>'
            f'<tbody>{body}</tbody></table>'
        )
        if rows
        else ''
    )
    legend = (
        (
            '<div class="legend"><span><span class="mode auto">AUTO</span>same verb, runs</span>'
            '<span><span class="mode ask">ASK</span>suggests only</span>'
            '<span><span class="mode honest">HONEST</span>explains the gate</span>'
            '<span><span class="mode clean">CLEAN</span>fix an entry</span>'
            '<span><span class="mode fixm">FIX</span>engine change</span></div>'
        )
        if rows
        else ''
    )
    return (
        f'<section class="view" id="v3"><h2 class="sr">Map changes</h2><p class="lede">{esc(lede)}</p>'
        f'{table}{more}{legend}</section>'
    )


def vocabulary_view(data: dict[str, Any]) -> str:
    vocab = data['rec']['vocabulary']
    if not vocab.get('rows'):
        return (
            '<section class="view" id="v4"><h2 class="sr">Flag vocabulary</h2>'
            '<p class="lede ok"><i class="sw"></i>No flag drift in this export.</p></section>'
        )
    counts = [int(v.split(':')[1]) for row in vocab['cells'].values() for v in row.values() if v.startswith('missing:')]
    peak = max(counts) if counts else 1

    def cell(value: str) -> str:
        if value == 'has':
            return '<td class="has">✓</td>'
        if value.startswith('missing:'):
            count = int(value.split(':')[1])
            step = 3 if count * 3 >= peak * 2 else 2 if count * 3 >= peak else 1
            return f'<td><span class="miss s{step}">{count}</span></td>'
        if value == 'add':
            return '<td><span class="add">add</span></td>'
        return '<td class="na">·</td>'

    head = ''.join(f'<th>{esc(flag)}</th>' for flag in vocab['columns'])
    body = ''.join(
        f'<tr><td>{code(path)}</td>' + ''.join(cell(vocab['cells'][path][flag]) for flag in vocab['columns']) + '</tr>'
        for path in vocab['rows']
    )
    cards = []
    for family in vocab.get('families', [])[:2]:
        if family['contract']:
            cards.append(
                f'<div class="card"><div class="k"><i class="sw gap"></i>Shared vocabulary</div>'
                f'<p>Every {code(family["verb"])}: {" ".join(code(f) for f in family["contract"][:4])}</p></div>'
            )
    if vocab.get('tokens'):
        pairs = ' · '.join(f'{code(t["pattern"])} → {code(t["target"])}' for t in vocab['tokens'][:3])
        cards.append(f'<div class="card"><div class="k">Suggest only</div><p>{pairs}</p></div>')
    legend = (
        '<div class="legend"><span class="has">✓ has it</span><span><span class="miss s3">n</span>requested, missing</span>'
        '<span><span class="add">add</span>shared vocabulary fills it</span></div>'
    )
    return (
        f'<section class="view" id="v4"><h2 class="sr">Flag vocabulary</h2>'
        '<p class="lede">Agents assume sibling commands share flags. They don\'t, yet.</p>'
        f'<table class="mx"><thead><tr><th>Command</th>{head}</tr></thead><tbody>{body}</tbody></table>{legend}'
        f'<div class="grid3">{"".join(cards[:3])}</div></section>'
    )


def privacy_view(data: dict[str, Any]) -> str:
    profile = data['profile']
    privacy = profile.get('privacy', {})
    total = profile['events'] or 1
    raw = profile.get('privacy_events', 0)
    if not privacy:
        return (
            '<section class="view" id="v5"><h2 class="sr">Privacy</h2>'
            '<p class="lede ok"><i class="sw"></i>No raw values in this export.</p></section>'
        )
    order = ['any-value'] + [name for name in privacy if name != 'any-value']
    rows = []
    for name in order:
        if name not in privacy:
            continue
        events = privacy[name]['events']
        rows.append(
            f'<span>{esc(privacy[name]["label"])}</span><span class="track"><span class="fill" style="width:{100 * events / total:.1f}%"></span></span>'
            f'<span class="v n">{events}</span>'
        )
    example = next(
        (s['argv'] for s in data['rec']['shapes'] if any(t.startswith('<') or '=<' in t for t in s['argv'])), None
    )
    columns = sorted({COLUMN_LABEL.get(c, c) for v in privacy.values() for c in v['columns']})
    top = [PRIVACY_SHORT[name] for name in privacy if name in PRIVACY_SHORT][:3]
    lede = (
        f'{raw} of {total} events carry raw values: {", ".join(top)}.'
        if raw
        else f'Only argument values: {privacy.get("any-value", {}).get("events", 0)} events.'
    )
    cards = (
        '<div class="card"><div class="k"><i class="sw defect"></i>Redact to the shape</div>'
        '<p>Keep command path and flag names. Replace every value.</p>'
        + (f'<p>{code(" ".join(example))}</p>' if example else '')
        + '</div>'
        f'<div class="card"><div class="k">Columns affected</div><p>{esc(", ".join(columns))}</p></div>'
    )
    return (
        f'<section class="view" id="v5"><h2 class="sr">Privacy</h2><p class="lede">{esc(lede)}</p>'
        f'<div class="bars">{"".join(rows)}</div><div class="grid3">{cards}</div></section>'
    )


# ---------------------------------------------------------------------------
# implementation notes


def notes(data: dict[str, Any]) -> str:
    cli, profile, rec, run = data['cli'], data['profile'], data['rec'], data['run']
    parts = ['<details class="notes"><summary>Implementation notes</summary><div class="body">']
    agent_types = ', '.join(f'{esc(k)} {v}' for k, v in profile.get('agent_types', {}).items()) or 'none recorded'
    actors = ', '.join(f'{esc(k)} {v}' for k, v in profile.get('actors', {}).items())
    versions = profile.get('versions', {})
    parts.append('<h3>Data and method</h3><ul>')
    parts.append(
        f'<li>Export: {profile["events"]} events ({esc(", ".join(f"{k} {v}" for k, v in profile.get("error_kinds", {}).items()))}); '
        f'actors {actors}; agent types {agent_types}; client versions {esc(versions.get("oldest", "?"))} to '
        f'{esc(versions.get("newest", "?"))}; {esc(profile.get("first_day", "?"))} to {esc(profile.get("last_day", "?"))}.</li>'
    )
    parts.append(
        f'<li>Normalized to {profile["shapes"]} anonymized argv shapes: command-path words and flag names kept, every '
        f'other token replaced by a typed placeholder ({code("<path>")}, {code("<id>")}, {code("<n>")}, {code("<text>")}, …).</li>'
    )
    parts.append(f'<li>{esc(run.get("method_note", ""))}</li>')
    if run.get('env_keys'):
        parts.append(
            f'<li>Side-effect switches set during replay (names only): {", ".join(code(k) for k in run["env_keys"])}.</li>'
        )
    parts.append(
        '<li>Buckets are assigned by ordered rules, first match wins; the AI coworker decided only the residue '
        f'({sum(1 for s in rec["shapes"] if s.get("decided"))} shapes).</li>'
    )
    parts.append('<li>This page carries counts and anonymized shapes only.</li></ul>')

    parts.append(
        '<h3>Buckets (table view of the summary chart)</h3><table><thead><tr><th>Bucket</th><th>Rule</th>'
        '<th class="n">Events</th><th class="n">Shapes</th><th class="n">Still fail</th></tr></thead><tbody>'
    )
    for bucket in BUCKETS:
        if bucket in rec['buckets']:
            values = rec['buckets'][bucket]
            parts.append(
                f'<tr><td><i class="sw {FAMILY[bucket]}"></i> {esc(bucket)}</td><td>{esc(BUCKET_RULE[bucket])}</td>'
                f'<td class="n">{values["events"]}</td><td class="n">{values["shapes"]}</td><td class="n">{values["still_fails"]}</td></tr>'
            )
    parts.append('</tbody></table>')

    if rec['catalog_entries']:
        parts.append(
            '<h3>Catalog entries</h3><table><thead><tr><th>Typed</th><th>Target</th><th>Mode</th><th>Why</th>'
            '<th>Simulated</th><th class="n">Events</th></tr></thead><tbody>'
        )
        for entry in rec['catalog_entries']:
            simulated = 'lands on target' if entry.get('verified') else 'check by hand'
            if entry.get('shadowed'):
                simulated = 'shadowed by a real command'
            parts.append(
                f'<tr><td>{code(entry["typed"])}</td><td>{code(entry["target"])}</td><td>{entry["mode"]}</td>'
                f'<td>{esc(entry["why"])}</td><td>{esc(simulated)}</td><td class="n">{entry["events"]}</td></tr>'
            )
        parts.append('</tbody></table>')
        parts.append(
            "<p>Paste into the catalog's <code>commands</code> and <code>tokens</code> arrays "
            '(also written to <code>catalog-patch.json</code>):</p>'
        )
        parts.append(f'<pre>{esc(json.dumps(rec["catalog_patch"], indent=2))}</pre>')
    if rec['catalog_cleanup']:
        parts.append('<h3>Existing entries to clean up</h3><ul>')
        for row in rec['catalog_cleanup']:
            target = f' → {code(row["target"])}' if row.get('target') else ''
            replaced = '; the new entry above replaces it' if row.get('replaced_by_entry') else ''
            parts.append(f'<li>{code(row["pattern"])}: {esc(row["action"])}{target} ({esc(row["why"])}{replaced})</li>')
        parts.append('</ul>')
    vocab = rec['vocabulary']
    if vocab.get('add'):
        parts.append('<h3>Flags to add</h3><ul>')
        for path, flags in sorted(vocab['add'].items()):
            parts.append(f'<li>{code(cli + " " + path)}: {" ".join(code(f) for f in flags)}</li>')
        parts.append('</ul>')
    if vocab.get('tokens'):
        parts.append('<h3>Token fixes (suggest-only)</h3><ul>')
        for token in vocab['tokens']:
            parts.append(f'<li>{code(token["pattern"])} → {code(token["target"])} ({token["count"]} events)</li>')
        parts.append('</ul>')
    if rec['strings_to_fix']:
        parts.append(
            '<h3>Strings to fix</h3><table><thead><tr><th>Where</th><th>Reference</th><th>Problem</th><th>Try</th></tr></thead><tbody>'
        )
        for row in rec['strings_to_fix']:
            hint = row['hint']
            hint_html = code(hint) if hint.startswith(cli + ' ') else esc(hint)
            parts.append(
                f'<tr><td>{code(row["where"])}</td><td>{code(row["ref"])}</td><td>{esc(row["problem"])}</td>'
                f'<td>{hint_html}</td></tr>'
            )
        parts.append('</tbody></table>')
    if rec['engine_fixes']:
        parts.append('<h3>Engine and CLI fixes</h3><ul>')
        for fix in rec['engine_fixes']:
            parts.append(f'<li><b>{esc(fix["title"])}</b> ({fix["events"]} events): {esc(fix["detail"])}</li>')
        parts.append('</ul>')
    parts.append('<h3>Tests to add (prove each red-first)</h3><ul>')
    for test in rec['tests']:
        parts.append(
            f'<li>{code(test["name"])} guards {esc(test["guards"])}: {esc(test["asserts"])} '
            f'<i>Red-first:</i> {esc(test["red_first"])}</li>'
        )
    parts.append(
        f'<li>The corpus lives in {code("corpus.json")}: one row per shape with its expected outcome.</li></ul>'
    )
    unverified = sum(s['count'] for s in rec['shapes'] if s['latest']['outcome'] == 'unverified')
    skipped = sum(
        s['count'] for s in rec['shapes'] if s['latest'].get('source') != 'replay' and run.get('replay') == 'exec'
    )
    parts.append('<h3>What could not be verified</h3><ul>')
    if rec['judgment']:
        parts.append(
            f'<li>{plural(len(rec["judgment"]), "shape")} still need a decision: '
            + ', '.join(code(j['key']) for j in rec['judgment'][:8])
            + '.</li>'
        )
    if unverified:
        parts.append(f'<li>{unverified} events were never checked against a binary.</li>')
    if skipped:
        parts.append(
            f'<li>{skipped} events kept their static outcome (destructive verb, timeout, or a non-parse error).</li>'
        )
    for problem in rec['decision_problems']:
        parts.append(f'<li>Decision file: {esc(problem)}</li>')
    parts.append(
        '<li>Unknown command words are kept verbatim because they are the signal; review before sharing.</li></ul>'
    )
    parts.append('</div></details>')
    return ''.join(parts)


# ---------------------------------------------------------------------------
# page


def headline(data: dict[str, Any]) -> str:
    cli, rec, profile = data['cli'], data['rec'], data['profile']
    total = profile['events']
    defect = sum(b['events'] for name, b in rec['buckets'].items() if FAMILY[name] == 'defect')
    if total and defect * 100 >= total * 15:
        return f"{defect} of {total} failures were {cli}'s own bugs"
    return f'What {total} failed {cli} invocations ask for'


def render(data: dict[str, Any]) -> str:
    cli, profile, run = data['cli'], data['profile'], data['run']
    entries = leak_entries(data)
    tabs = ''.join(
        f'<input type="radio" name="tab" id="t{i}"{" checked" if i == 1 else ""}><label for="t{i}">{name}</label>'
        for i, name in enumerate(('Summary', 'Where it leaks', 'Map changes', 'Flag vocabulary', 'Privacy'), 1)
    )
    days = f'{profile.get("first_day", "")} → {profile.get("last_day", "")}' if profile.get('first_day') else ''
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{esc(headline(data))}</title><style>{CSS}{leak_css(len(entries))}</style></head><body>'
        '<header class="top"><div class="wrap">'
        '<label class="theme"><input type="checkbox" id="theme-light"> Light</label>'
        f'<div class="eyebrow">Friction report · {esc(cli)}{" · " + esc(days) if days else ""}</div>'
        f'<h1>{esc(headline(data))}</h1><p>{esc(run.get("method_line", ""))}</p></div></header>'
        f'<nav class="tabs"><div class="wrap">{tabs}</div></nav><main class="wrap">'
        f'{summary_view(data)}{leaks_view(data)}{map_view(data)}{vocabulary_view(data)}{privacy_view(data)}'
        f'{notes(data)}<footer>Generated by the friction-analysis skill from an anonymized export. '
        'Counts and command shapes only.</footer></main></body></html>\n'
    )
    leak_check(page)
    return page


def leak_check(page: str) -> None:
    """Refuse to write a report that carries anything a raw export could have leaked."""
    detectors = dict(PRIVACY_DETECTORS)
    found = [name for name in LEAK_CHECKS if detectors[name].search(page)]
    if UUID.search(page):
        found.append('uuid')
    if '://' in page:
        found.append('url')
    if found:
        raise LeakError('report would contain raw values (' + ', '.join(found) + '); refusing to write it')
