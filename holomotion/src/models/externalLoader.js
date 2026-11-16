// Parses external .obj, .stl (ASCII & binary), and .glb files into surface-sampled GPU particle buffers.
import { makeRng } from '../lib/rng.js';
import { normalize } from '../lib/sampling.js';

/** Extract triangle vertices [x0,y0,z0, x1,y1,z1, x2,y2,z2, ...] from an OBJ string. */
function parseOBJ(text) {
  const verts = [];
  const tris = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const parts = line.split(/\s+/);
    if (parts[0] === 'v' && parts.length >= 4) {
      verts.push([parseFloat(parts[1]), parseFloat(parts[2]), parseFloat(parts[3])]);
    } else if (parts[0] === 'f' && parts.length >= 4) {
      const idx = parts.slice(1).map((tok) => {
        const vi = parseInt(tok.split('/')[0], 10);
        return vi < 0 ? verts.length + vi : vi - 1;
      });
      for (let i = 1; i + 1 < idx.length; i++) {
        const a = verts[idx[0]], b = verts[idx[i]], c = verts[idx[i + 1]];
        if (a && b && c) tris.push(a[0], a[1], a[2], b[0], b[1], b[2], c[0], c[1], c[2]);
      }
    }
  }
  return new Float32Array(tris);
}

/** Extract triangle vertices from a binary or ASCII STL ArrayBuffer. */
function parseSTL(buffer) {
  const view = new DataView(buffer);
  const isBinary = buffer.byteLength >= 84 && 84 + view.getUint32(80, true) * 50 === buffer.byteLength;
  if (isBinary) {
    const count = view.getUint32(80, true);
    const tris = new Float32Array(count * 9);
    let off = 84, o = 0;
    for (let i = 0; i < count; i++) {
      off += 12; // skip normal
      for (let v = 0; v < 9; v++) {
        tris[o++] = view.getFloat32(off, true);
        off += 4;
      }
      off += 2; // skip attribute byte count
    }
    return tris;
  }
  const text = new TextDecoder().decode(buffer);
  const tris = [];
  const re = /vertex\s+([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)\s+([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)\s+([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    tris.push(parseFloat(m[1]), parseFloat(m[2]), parseFloat(m[3]));
  }
  return new Float32Array(tris);
}

/** Extract triangle vertices from a glTF 2.0 Binary (.glb) ArrayBuffer. */
function parseGLB(buffer) {
  const view = new DataView(buffer);
  if (view.getUint32(0, true) !== 0x46546c67) throw new Error('Invalid GLB header');
  let off = 12, json = null, binOffset = 0;
  while (off < buffer.byteLength) {
    const chunkLen = view.getUint32(off, true);
    const chunkType = view.getUint32(off + 4, true);
    if (chunkType === 0x4e4f534a) {
      json = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, off + 8, chunkLen)));
    } else if (chunkType === 0x004e4942) {
      binOffset = off + 8;
    }
    off += 8 + chunkLen;
  }
  if (!json || !binOffset) throw new Error('Missing JSON or BIN chunk in GLB');

  const getAccessor = (accIdx) => {
    const acc = json.accessors[accIdx];
    const bv = json.bufferViews[acc.bufferView];
    const start = binOffset + (bv.byteOffset || 0) + (acc.byteOffset || 0);
    const comps = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4 }[acc.type] || 1;
    const len = acc.count * comps;
    if (acc.componentType === 5126) return new Float32Array(buffer, start, len);
    if (acc.componentType === 5123) return new Uint16Array(buffer, start, len);
    if (acc.componentType === 5125) return new Uint32Array(buffer, start, len);
    if (acc.componentType === 5121) return new Uint8Array(buffer, start, len);
    return null;
  };

  const tris = [];
  for (const mesh of json.meshes || []) {
    for (const prim of mesh.primitives || []) {
      if ((prim.mode ?? 4) !== 4 || prim.attributes?.POSITION === undefined) continue;
      const pos = getAccessor(prim.attributes.POSITION);
      if (!pos) continue;
      if (prim.indices !== undefined) {
        const idx = getAccessor(prim.indices);
        for (let i = 0; i + 2 < idx.length; i += 3) {
          const a = idx[i] * 3, b = idx[i + 1] * 3, c = idx[i + 2] * 3;
          tris.push(pos[a], pos[a + 1], pos[a + 2], pos[b], pos[b + 1], pos[b + 2], pos[c], pos[c + 1], pos[c + 2]);
        }
      } else {
        for (let i = 0; i + 8 < pos.length; i += 9) {
          tris.push(pos[i], pos[i + 1], pos[i + 2], pos[i + 3], pos[i + 4], pos[i + 5], pos[i + 6], pos[i + 7], pos[i + 8]);
        }
      }
    }
  }
  return new Float32Array(tris);
}

/** Area-weighted uniform surface sampling across triangles into `n` particles. */
function sampleTriangles(tris, n, seed = 42) {
  const triCount = Math.floor(tris.length / 9);
  if (triCount === 0) throw new Error('No 3D triangles found in file');

  const cdf = new Float64Array(triCount);
  let totalArea = 0;
  for (let i = 0; i < triCount; i++) {
    const o = i * 9;
    const ux = tris[o + 3] - tris[o], uy = tris[o + 4] - tris[o + 1], uz = tris[o + 5] - tris[o + 2];
    const vx = tris[o + 6] - tris[o], vy = tris[o + 7] - tris[o + 1], vz = tris[o + 8] - tris[o + 2];
    const cx = uy * vz - uz * vy, cy = uz * vx - ux * vz, cz = ux * vy - uy * vx;
    totalArea += 0.5 * Math.hypot(cx, cy, cz);
    cdf[i] = totalArea;
  }

  const rng = makeRng(seed);
  const target = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    const r = rng.random() * totalArea;
    let lo = 0, hi = triCount - 1;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (cdf[mid] < r) lo = mid + 1;
      else hi = mid;
    }
    let u = rng.random(), v = rng.random();
    if (u + v > 1) { u = 1 - u; v = 1 - v; }
    const w = 1 - u - v;
    const o = lo * 9, j = i * 3;
    target[j]     = w * tris[o]     + u * tris[o + 3] + v * tris[o + 6];
    target[j + 1] = w * tris[o + 1] + u * tris[o + 4] + v * tris[o + 7];
    target[j + 2] = w * tris[o + 2] + u * tris[o + 5] + v * tris[o + 8];
  }
  normalize(target);
  return target;
}

/** Load a File object (.obj, .stl, .glb) and build a Holomotion-compatible model buffer object. */
export async function loadExternalModelFile(file, n, index) {
  const t0 = performance.now();
  const ext = file.name.split('.').pop().toLowerCase();
  const buffer = await file.arrayBuffer();

  let tris;
  if (ext === 'obj') tris = parseOBJ(new TextDecoder().decode(buffer));
  else if (ext === 'stl') tris = parseSTL(buffer);
  else if (ext === 'glb') tris = parseGLB(buffer);
  else throw new Error(`Unsupported format ".${ext}". Please upload .glb, .obj, or .stl`);

  const target = sampleTriangles(tris, n, index * 7919 + 17);
  const color = [0.4, 0.88, 1.0];
  const offset = new Float32Array(n * 4);
  const tint = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) tint.set(color, i * 3);

  const cleanName = file.name.replace(/\.[^.]+$/, '');
  const def = {
    name: cleanName,
    kind: 'wonder',
    category: 'Custom',
    fact: `Uploaded ${ext.toUpperCase()} model (${Math.floor(tris.length / 9).toLocaleString()} triangles)`,
    color,
    tilt: 0,
  };

  return {
    index,
    def,
    n,
    target,
    offset,
    tint,
    pick: new Float32Array(n).fill(-1),
    labels: [],
    groups: [],
    fitScale: 1,
    exCenter: [0, 0, 0],
    ms: performance.now() - t0,
  };
}
