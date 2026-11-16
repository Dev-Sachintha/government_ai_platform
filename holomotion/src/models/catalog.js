import { makeRng } from '../lib/rng.js';
import { normalize } from '../lib/sampling.js';

/**
 * Generates procedural built-in particle buffers matching the externalLoader schema.
 */
export function buildBuiltinModels(n = 15000) {
  return [
    createSphereModel(0, n),
    createTorusModel(1, n)
  ];
}

function createSphereModel(index, n) {
  const rng = makeRng(101);
  const target = new Float32Array(n * 3);
  const color = [0.22, 0.74, 0.97];
  const tint = new Float32Array(n * 3);

  for (let i = 0; i < n; i++) {
    const u = rng.random();
    const v = rng.random();
    const theta = 2 * Math.PI * u;
    const phi = Math.acos(2 * v - 1);
    const j = i * 3;
    target[j] = Math.sin(phi) * Math.cos(theta);
    target[j + 1] = Math.sin(phi) * Math.sin(theta);
    target[j + 2] = Math.cos(phi);
    tint.set(color, j);
  }
  normalize(target);

  return {
    id: 'builtin-sphere',
    index,
    isUserModel: false,
    def: {
      name: 'Holo Sphere',
      kind: 'core',
      category: 'Built-in',
      fact: 'Procedural geodesic particle sphere.',
      color,
      tilt: 0
    },
    n,
    target,
    offset: new Float32Array(n * 4),
    tint,
    pick: new Float32Array(n).fill(-1),
    labels: [],
    groups: [],
    fitScale: 1,
    exCenter: [0, 0, 0],
    ms: 1.0
  };
}

function createTorusModel(index, n) {
  const rng = makeRng(202);
  const target = new Float32Array(n * 3);
  const color = [0.56, 0.88, 1.0];
  const tint = new Float32Array(n * 3);
  const R = 0.72, r = 0.28;

  for (let i = 0; i < n; i++) {
    const u = rng.random() * Math.PI * 2;
    const v = rng.random() * Math.PI * 2;
    const j = i * 3;
    target[j] = (R + r * Math.cos(v)) * Math.cos(u);
    target[j + 1] = r * Math.sin(v);
    target[j + 2] = (R + r * Math.cos(v)) * Math.sin(u);
    tint.set(color, j);
  }
  normalize(target);

  return {
    id: 'builtin-torus',
    index,
    isUserModel: false,
    def: {
      name: 'Quantum Ring',
      kind: 'core',
      category: 'Built-in',
      fact: 'Procedural toroidal harmonic field.',
      color,
      tilt: 0
    },
    n,
    target,
    offset: new Float32Array(n * 4),
    tint,
    pick: new Float32Array(n).fill(-1),
    labels: [],
    groups: [],
    fitScale: 1,
    exCenter: [0, 0, 0],
    ms: 1.0
  };
}
