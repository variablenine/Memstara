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
// pack() lays a box's transactions out as tiles, each one's volume its share of
// the box, so how high they stack is how full the block is.

export const COINBASE_RESERVE = 600;   // CRYPTONOTE_COINBASE_BLOB_RESERVED_SIZE: room kept for the miner's transaction
const FLAG_DOUBLE_SPEND = 1, FLAG_NOT_RELAYED = 2;   // pool.py

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
// wouldn't take as they stand (a double spend seen, or never sent on by your node).
export function pick(rows, maxWeight, base, skip = null) {
  const median = maxWeight / 2;
  const room = maxWeight - COINBASE_RESERVE;
  const cand = [], out = [];
  for (const r of rows || []) {
    if (!Array.isArray(r) || typeof r[0] !== "string" || !(r[3] > 0) || !(r[2] >= 0)) continue;
    if (skip && skip.has(r[0])) continue;
    if ((r[4] | 0) & (FLAG_DOUBLE_SPEND | FLAG_NOT_RELAYED)) out.push(r);
    else cand.push(r);
  }
  cand.sort((a, b) => b[2] / b[3] - a[2] / a[3] || a[1] - b[1] || (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
  const inside = [], left = [];
  let weight = 0, fee = 0, best = base;
  for (const r of cand) {
    if (weight + r[3] > room) { left.push(r); continue; }
    if (base !== null && base !== undefined) {
      const reward = rewardAt(base, median, weight + r[3]);
      if (reward === null || reward + fee + r[2] < best) { left.push(r); continue; }
      best = reward + fee + r[2];
    }
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
// Each tile stands on k x k columns, the fewest that keep it no taller than 1.5
// times its width, on the lowest such spot (the first one, row by row, on a tie).
// Returns {tiles: [{x0, x1, z0, z1, y0, y1}] (fractions of the box; y up from its
// floor), placed: how many were placed, top: the highest column}.
export function pack(fracs, g, lid = 0, stop = Infinity) {
  const h = new Float64Array(g * g).fill(lid);
  const tiles = [];
  for (const fr of fracs) {
    let k = 1;
    while (k < g && fr / ((k / g) ** 2) > 1.5 * (k / g)) k++;
    const tall = fr / ((k / g) ** 2);
    let bi = 0, bj = 0, base = Infinity;
    for (let i = 0; i + k <= g; i++) {
      for (let j = 0; j + k <= g; j++) {
        let m = 0;
        for (let a = i; a < i + k; a++) for (let b = j; b < j + k; b++) m = Math.max(m, h[a * g + b]);
        if (m < base - 1e-12) { base = m; bi = i; bj = j; }
      }
    }
    if (base >= stop) break;
    for (let a = bi; a < bi + k; a++) for (let b = bj; b < bj + k; b++) h[a * g + b] = base + tall;
    tiles.push({ x0: bi / g, x1: (bi + k) / g, z0: bj / g, z1: (bj + k) / g, y0: base, y1: base + tall });
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
