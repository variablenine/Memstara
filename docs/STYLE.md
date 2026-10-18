# Look and wording

The screens borrow the *general* look of cinematic "fictional user interface"
art (the kind collected on uispace.org's SCENESCAPES gallery, in particular the
mood of its "second party" scene: warm light and cool captions floating on
black, a live web of nodes, a scrolling log) and of real industrial control
rooms. No code, artwork, names or wording from that gallery is used: it is
© Annas Rahman with no licence to reuse, so everything here is written from
scratch and only follows the broad conventions below, most of which come from
real control-room practice (ISA-18.1 annunciators, ISA-101 "grey is normal"
screens, sequence-of-event logs).

## Rules

1. **AMOLED black ground.** The page and every panel are pure black (`#000`,
   pixels off on an OLED screen). Panels are drawn with light, not fills: 1px
   hairlines tinted toward the look, small corner ticks, and a soft glow.
2. **Grey is normal.** Healthy readings, unlit lamps, grids and axes stay in
   low-contrast neutrals. Colour means something.
3. **Heads-up and problem colours are reserved.** No chart series, accent or
   decoration ever uses them, and every heads-up or problem also carries a word
   (HEADS-UP, PROBLEM) and a glyph (▲, ■), never colour alone. Which colours they
   are depends on the look, so they never clash with its accent: amber look,
   coral heads-up and crimson problem; Monero orange look, yellow and crimson;
   the others, amber and red. Each trio was checked apart for normal and
   colour-blind sight with the dataviz palette checker. Problems pulse about once
   a second (lamp, overall chip, the screen's centrepiece frame); heads-ups glow
   steady; healthy things only breathe slowly.
4. **Cool colours are live data traces**, not "OK". Chart series use a validated
   pair: cyan `#1ea2b4` and violet `#8a78ec` (passes the lightness band, chroma,
   colour-blind and normal-vision separation, and 3:1 contrast checks). Text
   stays in ink; a coloured mark beside it carries which series it is.
5. **The accent is chrome and light** (panel ids, the open tab, the logo mark,
   focus rings, gauges, the node tree, the glow). Looks: amber (default: gold
   light, cyan headings, cyan-tinted hairlines, pale green log text), phosphor
   green, ice, bone, violet, Monero orange. CSS tokens: `--accent`, `--head`
   (panel titles), `--term` (log lines), `--warn`, `--bad` and their `-rgb` and
   `-ink` forms.
6. **Labels are short, uppercase, monospaced and widely spaced.** Parts are
   joined with a middle dot: `PEERS · OUT 30 / 32 · IN 31`. No jargon where a
   plain word works ("one company's servers", not a CIDR prefix; "blocks
   replaced", with "reorg" only as the short tag). Big numbers carry a soft glow
   in the accent.
7. **Numbers against their reference.** `30 / 32`, `120 · 30 RANGES`,
   `4.1 GB · LIMIT 20 GB`. Durations as `3 d 04 h`, `12 min`, `1m 42s`.
   Unknown is `—`, never 0.
8. **Say what is *not* happening.** `ALERTS 0 · NONE RAISED`, `LOG · EMPTY`,
   `NONE · NOTHING AT THE LIMITS`.
9. **Every panel has a letter or number, a title and a provenance line**: where
   the numbers come from, how often, how many are shown.
10. **Every chart has a one-sentence caption** saying how to read it, a legend
    when it has two series, and a table view or tooltip with the exact values.
11. **Quiet alarms.** No pop-ups. A problem lights its lamp, which flashes about
    once a second until acknowledged (an on-screen acknowledgement only; it
    changes nothing on the node). `TEST` lights every lamp for two seconds.
12. **Screens, each with a centrepiece.** Tabs across the top (STATUS, NETWORK,
    CHAIN, MEMPOOL, TRAFFIC, BANS, LOG) show one screen at a time,
    or all of them stack on one page (Settings → LOOK → Screens). Each screen
    opens with an animated centrepiece ("hero") drawn from real readings; the
    older panels follow it. A tab shows ▲ / ■ when its screen has an alert.
13. **Motion is honest and optional.** Anything that moves because of data says
    so (the tree's dots are real per-peer traffic); purely decorative motion
    (a sweep, drifting specks, the breathing glow) never imitates a reading.
    Scanlines, glow, motion and the start-up check all switch off in settings,
    and the desktop's reduce-animation setting always stills them. Centrepieces
    only animate while their screen is on show.
14. **Safe from stray keys.** Keyboard shortcuts are off by default; settings only change
    when SAVE is clicked; turning privacy mode off needs a typed word.
15. **Nothing leaves the machine.** Fonts are bundled (IBM Plex Mono and IBM
    Plex Sans Condensed, SIL Open Font License 1.1, licence files in
    `web/fonts/`), and the page loads nothing from any other host.
