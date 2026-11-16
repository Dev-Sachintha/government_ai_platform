/**
 * Deterministic Mulberry32 PRNG for reproducible surface sampling.
 * @param {number} seed
 */
export function makeRng(seed = 42) {
  let a = seed >>> 0;
  return {
    random() {
      a |= 0;
      a = (a + 0x6D2B79F5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    }
  };
}
