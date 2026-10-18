// The next block as your node's pool would fill it, for the CHAIN screen. Plain
// functions with no drawing, so they can be checked on their own
// (tests/test_fill.py runs them under node).
//
// pick() takes the waiting transactions in the order monerod takes them when it
// builds a block (src/cryptonote_core/tx_pool.cpp, fill_block_template, and
// txCompare in tx_pool.h, release-v0.18): the most fee per byte first, then the
// one that has waited longest. It adds each one that still fits in the biggest
// block allowed (less the 600 bytes kept for the miner's own transaction). Past
// the full-reward line (half the biggest block) a bigger block earns the miner a
// smaller reward, so a transaction goes in there only while its fee makes up for
// what the reward loses (get_block_reward in cryptonote_basic_impl.cpp).
//
// A transaction your node marked as a double spend still goes in: monerod
// doesn't look at that mark when it builds a block (it was set on the one already
// waiting when a second one tried to spend the same thing, and the first one is
// still good). One your node hasn't passed on yet is left out, as monerod leaves
// out what it hasn't sent on to the network.
//
// pack() lays a box's transactions out as tiles, each one's volume its share of
// the box, so how high they stack is how full the block is.

export const COINBASE_RESERVE = 600;   // CRYPTONOTE_COINBASE_BLOB_RESERVED_SIZE: room kept for the miner's transaction
const FLAG_NOT_RELAYED = 2;   // pool.py

// the reward a block of `weight` bytes earns, given the reward with no penalty
// (base) and the median: full up to the median, less and less up to twice it,
// and no block at all past that (null)
export function rewardAt(base, median, weight) {
  if (weight <= median) return base;
  if (weight > 2 * median) return null;
  return (base * (2 * median - weight) * weight) / (median * median);
}

// mempool.list rows ([id, arrived, fee, weight, flags]) -> what the next block would hold.
// maxWeight: the biggest block allowed (get_info.block_weight_limit, twice the median);
// base: the block reward without fees or penalty (null to skip the reward check).
// skip: ids to leave out (already in a block).
// Returns {inside, left, out, weight, fee}: inside and left in the order taken
// (left: what doesn't fit or isn't worth it to the miner); out: rows monerod
// wouldn't take as they stand (not passed on by your node yet).
export function pick(rows, maxWeight, base, skip = null) {
  const median = maxWeight / 2;
  const room = maxWeight - COINBASE_RESERVE;
  const cand = [], out = [];
  for (const r of rows || []) {
    if (!Array.isArray(r) || typeof r[0] !== "string" || !(r[3] > 0) || !(r[2] >= 0)) continue;
    if (skip && skip.has(r[0])) continue;
    if ((r[4] | 0) & FLAG_NOT_RELAYED) out.push(r);
    else cand.push(r);
  }
  cand.sort((a, b) => b[2] / b[3] - a[2] / a[3] || a[1] - b[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  const inside = [], left = [];
  const known = base !== null && base !== undefined;
  let weight = 0, fee = 0, best = base;
  for (const r of cand) {
    if (weight + r[3] > room) { left.push(r); continue; }
    if (known) {
      // monerod compares against the best so far as a 32-bit float
      // (template_accept_threshold), so a sliver under it still counts
      const reward = rewardAt(base, median, weight + r[3]);
      if (reward === null || reward + fee + r[2] < Math.fround(best)) { left.push(r); continue; }
      best = reward + fee + r[2];
    } else if (weight + r[3] > median) { left.push(r); continue; }   // the reward isn't known: not past the full-reward line
    inside.push(r);
    weight += r[3];
    fee += r[2];
  }
  return { inside, left, out, weight, fee };
}

// fracs: each tile's volume as a share of the box (in the order they go in);
// g: columns across each side of the floor; lid: the height every column starts
// at (1 = the top of the box, for what stacks above it); stop: no tile starts at
// or above this height (the rest aren't placed).
// Each tile stands on k x k columns, no taller than 1.5 times its width and no
// flatter than a tenth of it (as near as the floor allows), at the spot and width
// that leave its top lowest (on a tie the narrower, then the first one, row by row),
// so the stack stays close to how full the box is.
// Returns {tiles: [{x0, x1, z0, z1, y0, y1}] (fractions of the box; y up from its
// floor), placed: how many were placed, top: the highest column}.
export function pack(fracs, g, lid = 0, stop = Infinity) {
  const h = new Float64Array(g * g).fill(lid);
  const tiles = [];
  const tallAt = (fr, k) => fr / ((k / g) ** 2);
  for (const fr of fracs) {
    let k0 = 1;
    while (k0 < g && tallAt(fr, k0) > 1.5 * (k0 / g)) k0++;
    let k1 = k0;
    while (k1 < g && tallAt(fr, k1 + 1) >= 0.1 * ((k1 + 1) / g)) k1++;
    let bi = 0, bj = 0, bk = k0, base = Infinity, top = Infinity;
    for (let k = k0; k <= k1; k++) {
      const tall = tallAt(fr, k);
      for (let i = 0; i + k <= g; i++) {
        for (let j = 0; j + k <= g; j++) {
          let m = 0;
          for (let a = i; a < i + k; a++) for (let b = j; b < j + k; b++) m = Math.max(m, h[a * g + b]);
          if (m + tall < top - 1e-12) { top = m + tall; base = m; bi = i; bj = j; bk = k; }
        }
      }
    }
    if (base >= stop) break;
    const tall = tallAt(fr, bk);
    for (let a = bi; a < bi + bk; a++) for (let b = bj; b < bj + bk; b++) h[a * g + b] = base + tall;
    tiles.push({ x0: bi / g, x1: (bi + bk) / g, z0: bj / g, z1: (bj + bk) / g, y0: base, y1: base + tall });
  }
  let top = lid;
  for (const v of h) top = Math.max(top, v);
  return { tiles, placed: tiles.length, top };
}

// tiles packed past a height (columns left uneven leave gaps under some) squeezed
// back down so the stack stands no higher than `to`
export function squeeze(res, from, to) {
  if (res.top <= to) return res;
  const k = (to - from) / (res.top - from);
  for (const t of res.tiles) { t.y0 = from + (t.y0 - from) * k; t.y1 = from + (t.y1 - from) * k; }
  res.top = to;
  return res;
}

// a block's tiles in the order taken, the ones up to `line` (the full-reward line,
// half the box) packed below it and the rest from there up to the top, so a tile
// stands above the line only when it was taken past it. Returns pack()'s shape.
export function stack(fracs, g, line = 0.5) {
  let n = 0, w = 0;
  while (n < fracs.length && w + fracs[n] <= line + 1e-12) w += fracs[n++];
  const a = squeeze(pack(fracs.slice(0, n), g), 0, line);
  const b = squeeze(pack(fracs.slice(n), g, a.top), a.top, 1);
  const tiles = [...a.tiles, ...b.tiles];
  return { tiles, placed: tiles.length, top: n < fracs.length ? b.top : a.top };
}
