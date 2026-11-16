// Importers for user-supplied 3D files: GLB / glTF (self-contained), OBJ, STL (binary + ASCII), PLY (ASCII + binary).
// Each importer returns a "soup": { tris: Float32Array(9*T), triCol: Float32Array(3*T) (-1 = unknown), pts, ptCol }.
// sampleMesh() then turns the soup into exactly n glowing particles (area-weighted over the surface).
import { normalize } from './sampling.js';

const MAX_TRIS = 6_000_000;

class Buf {
  constructor() { this.a = new Float32Array(1 << 16); this.n = 0; }
  ensure(k) { if (this.n + k > this.a.length) { const b = new Float32Array(Math.max(this.a.length * 2, this.n + k)); b.set(this.a.subarray(0, this.n)); this.a = b; } }
  add3(x, y, z) { this.ensure(3); const a = this.a, n = this.n; a[n] = x; a[n + 1] = y; a[n + 2] = z; this.n = n + 3; }
  done() { return this.a.slice(0, this.n); }
}
class Soup {
  constructor() { this.tris = new Buf(); this.triCol = new Buf(); this.pts = new Buf(); this.ptCol = new Buf(); }
  tri(a, b, c, col) {
    if (this.tris.n / 9 >= MAX_TRIS) return;
    this.tris.add3(a[0], a[1], a[2]); this.tris.add3(b[0], b[1], b[2]); this.tris.add3(c[0], c[1], c[2]);
    this.triCol.add3(col ? col[0] : -1, col ? col[1] : -1, col ? col[2] : -1);
  }
  pt(p, col) { this.pts.add3(p[0], p[1], p[2]); this.ptCol.add3(col ? col[0] : -1, col ? col[1] : -1, col ? col[2] : -1); }
  done() { return { tris: this.tris.done(), triCol: this.triCol.done(), pts: this.pts.done(), ptCol: this.ptCol.done() }; }
}

export async function parseMesh(buf, ext) {
  ext = String(ext || '').toLowerCase();
  let mesh;
  if (ext === 'glb' || ext === 'gltf') mesh = await parseGltf(buf, ext);
  else if (ext === 'obj') mesh = parseObj(new TextDecoder().decode(buf));
  else if (ext === 'stl') mesh = parseStl(buf);
  else if (ext === 'ply') mesh = parsePly(buf);
  else throw new Error(`Unsupported file type ".${ext}". Use .glb, .gltf, .obj, .stl or .ply`);
  if (!mesh.tris.length && !mesh.pts.length) throw new Error('No geometry found in this file.');
  return mesh;
}

// ---------------------------------------------------------------- OBJ
function parseObj(text) {
  const v = [], vc = [], faces = [];
  let hasVC = false;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (line.charCodeAt(0) === 118 && line.charCodeAt(1) === 32) {          // "v "
      const p = line.split(/\s+/);
      v.push(+p[1], +p[2], +p[3]);
      if (p.length >= 7) { vc.push(+p[4], +p[5], +p[6]); hasVC = true; } else vc.push(-1, -1, -1);
    } else if (line.charCodeAt(0) === 102 && line.charCodeAt(1) === 32) {   // "f "
      const idx = line.split(/\s+/).slice(1).map((t) => { const i = parseInt(t, 10); return i < 0 ? v.length / 3 + i : i - 1; });
      for (let k = 1; k + 1 < idx.length; k++) faces.push(idx[0], idx[k], idx[k + 1]);
    }
  }
  const s = new Soup();
  const P = (i) => [v[i * 3], v[i * 3 + 1], v[i * 3 + 2]];
  if (faces.length) {
    for (let f = 0; f < faces.length; f += 3) {
      const [a, b, c] = [faces[f], faces[f + 1], faces[f + 2]];
      if (!(a >= 0 && b >= 0 && c >= 0 && a * 3 < v.length && b * 3 < v.length && c * 3 < v.length)) continue;
      let col = null;
      if (hasVC) col = [0, 1, 2].map((k) => (vc[a * 3 + k] + vc[b * 3 + k] + vc[c * 3 + k]) / 3);
      s.tri(P(a), P(b), P(c), col);
    }
  } else for (let i = 0; i < v.length / 3; i++) s.pt(P(i), hasVC ? [vc[i * 3], vc[i * 3 + 1], vc[i * 3 + 2]] : null);
  return s.done();
}

// ---------------------------------------------------------------- STL
function parseStl(buf) {
  const s = new Soup(), dv = new DataView(buf);
  const binary = buf.byteLength >= 84 && 84 + dv.getUint32(80, true) * 50 === buf.byteLength;
  if (binary) {
    const T = dv.getUint32(80, true);
    for (let i = 0; i < T; i++) {
      const o = 84 + i * 50 + 12, f = (k) => dv.getFloat32(o + k * 4, true);
      s.tri([f(0), f(1), f(2)], [f(3), f(4), f(5)], [f(6), f(7), f(8)], null);
    }
  } else {
    const text = new TextDecoder().decode(buf), re = /vertex\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)/g;
    const vs = []; let m;
    while ((m = re.exec(text))) vs.push([+m[1], +m[2], +m[3]]);
    for (let i = 0; i + 2 < vs.length; i += 3) s.tri(vs[i], vs[i + 1], vs[i + 2], null);
  }
  return s.done();
}

// ---------------------------------------------------------------- PLY
const PLY_TYPES = {
  char: [1, 'getInt8'], int8: [1, 'getInt8'], uchar: [1, 'getUint8'], uint8: [1, 'getUint8'],
  short: [2, 'getInt16'], int16: [2, 'getInt16'], ushort: [2, 'getUint16'], uint16: [2, 'getUint16'],
  int: [4, 'getInt32'], int32: [4, 'getInt32'], uint: [4, 'getUint32'], uint32: [4, 'getUint32'],
  float: [4, 'getFloat32'], float32: [4, 'getFloat32'], double: [8, 'getFloat64'], float64: [8, 'getFloat64'],
};
function parsePly(buf) {
  const u8 = new Uint8Array(buf), head = new TextDecoder('latin1').decode(u8.subarray(0, Math.min(u8.length, 65536)));
  const endIdx = head.indexOf('end_header');
  if (endIdx < 0 || !head.startsWith('ply')) throw new Error('Not a valid PLY file.');
  let dataStart = endIdx + 'end_header'.length;
  if (head[dataStart] === '\r') dataStart++;
  if (head[dataStart] === '\n') dataStart++;
  const lines = head.slice(0, endIdx).split(/\r?\n/);
  let format = 'ascii'; const elements = [];
  for (const ln of lines) {
    const p = ln.trim().split(/\s+/);
    if (p[0] === 'format') format = p[1];
    else if (p[0] === 'element') elements.push({ name: p[1], count: +p[2], props: [] });
    else if (p[0] === 'property' && elements.length) {
      const e = elements[elements.length - 1];
      if (p[1] === 'list') e.props.push({ list: true, cType: p[2], type: p[3], name: p[4] });
      else e.props.push({ list: false, type: p[1], name: p[2] });
    }
  }
  const little = format !== 'binary_big_endian', ascii = format === 'ascii';
  const dv = new DataView(buf);
  let pos = dataStart, toks = null, ti = 0;
  if (ascii) { toks = new TextDecoder().decode(u8.subarray(dataStart)).split(/\s+/).filter(Boolean); }
  const read = (type) => {
    if (ascii) return +toks[ti++];
    const [sz, fn] = PLY_TYPES[type] || [4, 'getFloat32'];
    const val = sz === 1 ? dv[fn](pos) : dv[fn](pos, little);
    pos += sz; return val;
  };
  const verts = [], cols = [], faces = [];
  let anyCol = false;
  for (const el of elements) {
    for (let i = 0; i < el.count; i++) {
      const rec = {}; let list = null;
      for (const pr of el.props) {
        if (pr.list) {
          const n = read(pr.cType), arr = new Array(n);
          for (let k = 0; k < n; k++) arr[k] = read(pr.type);
          if (pr.name === 'vertex_indices' || pr.name === 'vertex_index') list = arr;
        } else rec[pr.name] = [read(pr.type), pr.type];
      }
      if (el.name === 'vertex') {
        verts.push(rec.x[0], rec.y[0], rec.z[0]);
        const r = rec.red || rec.r || rec.diffuse_red, g = rec.green || rec.g || rec.diffuse_green, b = rec.blue || rec.b || rec.diffuse_blue;
        if (r && g && b) {
          const sc = /float|double/.test(r[1]) ? 1 : 1 / 255;
          cols.push(r[0] * sc, g[0] * sc, b[0] * sc); anyCol = true;
        } else cols.push(-1, -1, -1);
      } else if (el.name === 'face' && list) {
        for (let k = 1; k + 1 < list.length; k++) faces.push(list[0], list[k], list[k + 1]);
      }
    }
  }
  const s = new Soup(), P = (i) => [verts[i * 3], verts[i * 3 + 1], verts[i * 3 + 2]];
  if (faces.length) {
    for (let f = 0; f < faces.length; f += 3) {
      const [a, b, c] = [faces[f], faces[f + 1], faces[f + 2]];
      if (a * 3 >= verts.length || b * 3 >= verts.length || c * 3 >= verts.length) continue;
      s.tri(P(a), P(b), P(c), anyCol ? [0, 1, 2].map((k) => (cols[a * 3 + k] + cols[b * 3 + k] + cols[c * 3 + k]) / 3) : null);
    }
  } else for (let i = 0; i < verts.length / 3; i++) s.pt(P(i), anyCol ? [cols[i * 3], cols[i * 3 + 1], cols[i * 3 + 2]] : null);
  return s.done();
}

// ---------------------------------------------------------------- glTF / GLB
const mat = {
  ident: () => { const m = new Float64Array(16); m[0] = m[5] = m[10] = m[15] = 1; return m; },
  mul: (a, b) => { const o = new Float64Array(16); for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) { let s = 0; for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k]; o[c * 4 + r] = s; } return o; },
  trs: (t = [0, 0, 0], q = [0, 0, 0, 1], s = [1, 1, 1]) => {
    const [x, y, z, w] = q, m = new Float64Array(16);
    m[0] = (1 - 2 * (y * y + z * z)) * s[0]; m[1] = 2 * (x * y + z * w) * s[0]; m[2] = 2 * (x * z - y * w) * s[0];
    m[4] = 2 * (x * y - z * w) * s[1]; m[5] = (1 - 2 * (x * x + z * z)) * s[1]; m[6] = 2 * (y * z + x * w) * s[1];
    m[8] = 2 * (x * z + y * w) * s[2]; m[9] = 2 * (y * z - x * w) * s[2]; m[10] = (1 - 2 * (x * x + y * y)) * s[2];
    m[12] = t[0]; m[13] = t[1]; m[14] = t[2]; m[15] = 1; return m;
  },
};

async function parseGltf(buf, ext) {
  let json, bin = null;
  if (ext === 'glb') {
    const dv = new DataView(buf);
    if (dv.getUint32(0, true) !== 0x46546c67) throw new Error('Not a valid GLB file.');
    let off = 12;
    while (off + 8 <= buf.byteLength) {
      const len = dv.getUint32(off, true), type = dv.getUint32(off + 4, true), data = new Uint8Array(buf, off + 8, len);
      if (type === 0x4e4f534a) json = JSON.parse(new TextDecoder().decode(data));
      else if (type === 0x004e4942 && !bin) bin = data;
      off += 8 + len + ((4 - (len % 4)) % 4);
    }
  } else json = JSON.parse(new TextDecoder().decode(buf));
  if (!json) throw new Error('glTF JSON not found.');
  const req = json.extensionsRequired || [];
  if (req.includes('KHR_draco_mesh_compression') || req.includes('EXT_meshopt_compression')) {
    throw new Error('This glTF uses Draco/Meshopt compression. Re-export it without compression.');
  }
  const buffers = (json.buffers || []).map((b, i) => {
    if (b.uri === undefined && i === 0 && bin) return bin;
    if (b.uri && b.uri.startsWith('data:')) { const s = atob(b.uri.split(',')[1]), u = new Uint8Array(s.length); for (let k = 0; k < s.length; k++) u[k] = s.charCodeAt(k); return u; }
    throw new Error('This .gltf points to an external .bin file. Use a .glb file (or a self-contained .gltf).');
  });
  const CSIZE = { 5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4 }, NCOMP = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4, MAT4: 16 };
  const NORM = { 5120: 127, 5121: 255, 5122: 32767, 5123: 65535 };
  const accessor = (ai) => {
    const a = json.accessors[ai], bv = json.bufferViews[a.bufferView], b = buffers[bv.buffer];
    const nc = NCOMP[a.type], cs = CSIZE[a.componentType], stride = bv.byteStride || nc * cs;
    const base = (bv.byteOffset || 0) + (a.byteOffset || 0), dv = new DataView(b.buffer, b.byteOffset, b.byteLength);
    const out = new Float64Array(a.count * nc);
    for (let i = 0; i < a.count; i++) for (let c = 0; c < nc; c++) {
      const o = base + i * stride + c * cs; let val;
      switch (a.componentType) {
        case 5120: val = dv.getInt8(o); break; case 5121: val = dv.getUint8(o); break;
        case 5122: val = dv.getInt16(o, true); break; case 5123: val = dv.getUint16(o, true); break;
        case 5125: val = dv.getUint32(o, true); break; default: val = dv.getFloat32(o, true);
      }
      out[i * nc + c] = a.normalized && NORM[a.componentType] ? Math.max(val / NORM[a.componentType], -1) : val;
    }
    return { data: out, nc };
  };

  const s = new Soup();
  const addMesh = (mesh, M) => {
    for (const p of mesh.primitives || []) {
      if (p.attributes?.POSITION === undefined) continue;
      const pos = accessor(p.attributes.POSITION), count = pos.data.length / 3;
      const W = new Float64Array(count * 3);
      for (let i = 0; i < count; i++) {
        const x = pos.data[i * 3], y = pos.data[i * 3 + 1], z = pos.data[i * 3 + 2];
        W[i * 3] = M[0] * x + M[4] * y + M[8] * z + M[12]; W[i * 3 + 1] = M[1] * x + M[5] * y + M[9] * z + M[13]; W[i * 3 + 2] = M[2] * x + M[6] * y + M[10] * z + M[14];
      }
      const col = p.attributes.COLOR_0 !== undefined ? accessor(p.attributes.COLOR_0) : null;
      let flat = null;
      const bc = json.materials?.[p.material]?.pbrMetallicRoughness?.baseColorFactor;
      if (!col && bc && !(bc[0] > 0.95 && bc[1] > 0.95 && bc[2] > 0.95)) flat = [bc[0], bc[1], bc[2]];
      const V = (i) => [W[i * 3], W[i * 3 + 1], W[i * 3 + 2]];
      const C = (i) => (col ? [col.data[i * col.nc], col.data[i * col.nc + 1], col.data[i * col.nc + 2]] : null);
      const idx = p.indices !== undefined ? accessor(p.indices).data : null, mode = p.mode ?? 4;
      const total = idx ? idx.length : count, I = (k) => (idx ? idx[k] : k);
      if (mode === 0) { for (let k = 0; k < total; k++) s.pt(V(I(k)), C(I(k)) || flat); continue; }
      const emit = (a, b, c) => {
        let cc = flat;
        if (col) { const ca = C(a), cb = C(b), cd = C(c); cc = [0, 1, 2].map((k) => (ca[k] + cb[k] + cd[k]) / 3); }
        s.tri(V(a), V(b), V(c), cc);
      };
      if (mode === 4) for (let k = 0; k + 2 < total; k += 3) emit(I(k), I(k + 1), I(k + 2));
      else if (mode === 5) for (let k = 0; k + 2 < total; k++) emit(I(k), I(k + 1), I(k + 2));
      else if (mode === 6) for (let k = 1; k + 1 < total; k++) emit(I(0), I(k), I(k + 1));
    }
  };
  const nodes = json.nodes || [];
  const nodeM = (n) => (n.matrix ? Float64Array.from(n.matrix) : mat.trs(n.translation, n.rotation, n.scale));
  const walk = (ni, pm, depth = 0) => {
    if (depth > 64) return;
    const n = nodes[ni], m = mat.mul(pm, nodeM(n));
    if (n.mesh !== undefined) addMesh(json.meshes[n.mesh], m);
    (n.children || []).forEach((c) => walk(c, m, depth + 1));
  };
  const scene = json.scenes?.[json.scene ?? 0];
  if (nodes.length) {
    let roots = scene?.nodes;
    if (!roots) { const child = new Set(nodes.flatMap((n) => n.children || [])); roots = nodes.map((_, i) => i).filter((i) => !child.has(i)); }
    roots.forEach((r) => walk(r, mat.ident()));
  } else (json.meshes || []).forEach((m) => addMesh(m, mat.ident()));
  return s.done();
}

// ---------------------------------------------------------------- sampling
/** Turn a soup into exactly n particles: { target (xyz, normalised to the unit globe), tint (rgb) }. */
export function sampleMesh(mesh, n, rngObj, defColor = [0.4, 0.88, 1]) {
  const rng = typeof rngObj === 'function' ? rngObj : () => rngObj.random();
  const target = new Float32Array(n * 3), tint = new Float32Array(n * 3);
  const T = mesh.tris.length / 9, P = mesh.pts.length / 3;
  const setTint = (i, c, o) => { const ok = c[o] >= 0; tint[i * 3] = ok ? c[o] : defColor[0]; tint[i * 3 + 1] = ok ? c[o + 1] : defColor[1]; tint[i * 3 + 2] = ok ? c[o + 2] : defColor[2]; };
  let usedTris = false;
  if (T > 0) {
    const cum = new Float64Array(T); let A = 0;
    const t = mesh.tris;
    for (let i = 0; i < T; i++) {
      const o = i * 9;
      const ux = t[o + 3] - t[o], uy = t[o + 4] - t[o + 1], uz = t[o + 5] - t[o + 2];
      const vx = t[o + 6] - t[o], vy = t[o + 7] - t[o + 1], vz = t[o + 8] - t[o + 2];
      const cx = uy * vz - uz * vy, cy = uz * vx - ux * vz, cz = ux * vy - uy * vx;
      A += 0.5 * Math.hypot(cx, cy, cz); cum[i] = A;
    }
    if (A > 0) {
      usedTris = true;
      for (let i = 0; i < n; i++) {
        const r = rng() * A; let lo = 0, hi = T - 1;
        while (lo < hi) { const mid = (lo + hi) >> 1; if (cum[mid] < r) lo = mid + 1; else hi = mid; }
        let u = rng(), v = rng(); if (u + v > 1) { u = 1 - u; v = 1 - v; }
        const o = lo * 9;
        for (let k = 0; k < 3; k++) target[i * 3 + k] = t[o + k] + u * (t[o + 3 + k] - t[o + k]) + v * (t[o + 6 + k] - t[o + k]);
        setTint(i, mesh.triCol, lo * 3);
      }
    }
  }
  if (!usedTris) {
    // point cloud (or degenerate mesh): use the vertices
    const src = P > 0 ? mesh.pts : mesh.tris, cols = P > 0 ? mesh.ptCol : null, m = src.length / 3;
    for (let i = 0; i < n; i++) {
      const j = Math.floor(rng() * m);
      for (let k = 0; k < 3; k++) target[i * 3 + k] = src[j * 3 + k];
      if (cols) setTint(i, cols, j * 3); else setTint(i, [-1], 0);
    }
  }
  normalize(target);
  if (!usedTris && P < n) { for (let i = 0; i < n * 3; i++) target[i] += (rng() - 0.5) * 0.008; }
  return { target, tint };
}
