import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { loadExternalModelFile } from './lib/externalLoader.js';
import {
  saveUserParticleModel,
  getUserParticleModels,
  deleteUserParticleModel
} from './lib/userModels.js';
import { buildBuiltinModels } from './models/catalog.js';

const PARTICLE_COUNT = 15000;

class HoloMotionParticleApp {
  constructor() {
    this.canvas = document.getElementById('viewport');
    this.modelSelect = document.getElementById('model-select');
    this.addModelBtn = document.getElementById('add-model-btn');
    this.fileInput = document.getElementById('model-file-input');
    this.removeModelBtn = document.getElementById('remove-model-btn');
    this.factBox = document.getElementById('model-fact');
    this.statusText = document.getElementById('status-text');
    this.particleCountEl = document.getElementById('particle-count');

    this.models = [];
    this.activeIndex = 0;

    // Particle morphing buffers
    this.currentPositions = new Float32Array(PARTICLE_COUNT * 3);
    this.targetPositions = new Float32Array(PARTICLE_COUNT * 3);
    this.currentColors = new Float32Array(PARTICLE_COUNT * 3);
    this.targetColors = new Float32Array(PARTICLE_COUNT * 3);

    this.initScene();
    this.initParticleSystem();
    this.bindEvents();
    this.bootstrapModels();
    this.animate();
  }

  initScene() {
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x050811);

    this.camera = new THREE.PerspectiveCamera(
      50,
      window.innerWidth / window.innerHeight,
      0.05,
      100
    );
    this.camera.position.set(0, 0.8, 3.2);

    this.renderer = new THREE.WebGLRenderer({
      canvas: this.canvas,
      antialias: true
    });
    this.renderer.setSize(window.innerWidth, window.innerHeight);
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.06;

    const grid = new THREE.GridHelper(6, 24, 0x1e293b, 0x0f172a);
    grid.position.y = -1.1;
    this.scene.add(grid);
  }

  initParticleSystem() {
    this.geometry = new THREE.BufferGeometry();
    this.geometry.setAttribute('position', new THREE.BufferAttribute(this.currentPositions, 3));
    this.geometry.setAttribute('color', new THREE.BufferAttribute(this.currentColors, 3));

    this.material = new THREE.PointsMaterial({
      size: 0.022,
      vertexColors: true,
      transparent: true,
      opacity: 0.9,
      blending: THREE.AdditiveBlending,
      depthWrite: false
    });

    this.points = new THREE.Points(this.geometry, this.material);
    this.scene.add(this.points);
    this.particleCountEl.textContent = `${PARTICLE_COUNT.toLocaleString()} pts`;
  }

  bindEvents() {
    window.addEventListener('resize', () => {
      this.camera.aspect = window.innerWidth / window.innerHeight;
      this.camera.updateProjectionMatrix();
      this.renderer.setSize(window.innerWidth, window.innerHeight);
    });

    this.modelSelect.addEventListener('change', (e) => {
      this.selectModel(Number(e.target.value));
    });

    this.addModelBtn.addEventListener('click', () => {
      this.fileInput.click();
    });

    this.fileInput.addEventListener('change', async (e) => {
      const file = e.target.files?.[0];
      if (!file) return;
      await this.handleExternalUpload(file);
      this.fileInput.value = '';
    });

    this.removeModelBtn.addEventListener('click', async () => {
      const current = this.models[this.activeIndex];
      if (!current || !current.isUserModel) return;

      await deleteUserParticleModel(current.id);
      await this.reloadCatalog();
      this.selectModel(0);
    });
  }

  async bootstrapModels() {
    await this.reloadCatalog();
    if (this.models.length > 0) {
      // Snap initial positions immediately
      this.currentPositions.set(this.models[0].target);
      this.currentColors.set(this.models[0].tint);
      this.selectModel(0);
    }
  }

  async reloadCatalog() {
    const builtins = buildBuiltinModels(PARTICLE_COUNT);
    const savedCustom = await getUserParticleModels();

    this.models = [...builtins, ...savedCustom].map((m, idx) => ({
      ...m,
      index: idx
    }));

    this.modelSelect.innerHTML = '';
    for (const m of this.models) {
      const opt = document.createElement('option');
      opt.value = String(m.index);
      opt.textContent = m.isUserModel ? `[Custom] ${m.def.name}` : m.def.name;
      this.modelSelect.appendChild(opt);
    }
  }

  async handleExternalUpload(file) {
    try {
      this.setStatus(`Sampling ${file.name}...`);
      const parsedModel = await loadExternalModelFile(file, PARTICLE_COUNT, this.models.length);
      const savedRecord = await saveUserParticleModel(parsedModel);

      await this.reloadCatalog();
      const newIdx = this.models.findIndex((m) => m.id === savedRecord.id);
      this.selectModel(newIdx >= 0 ? newIdx : this.models.length - 1);
      this.setStatus(`Loaded (${parsedModel.ms.toFixed(1)} ms)`);
    } catch (err) {
      console.error('Upload error:', err);
      this.setStatus(`Error: ${err.message}`);
    }
  }

  selectModel(index) {
    const model = this.models[index];
    if (!model) return;

    this.activeIndex = index;
    this.modelSelect.value = String(index);
    this.targetPositions.set(model.target);
    this.targetColors.set(model.tint);

    this.factBox.textContent = model.def.fact || model.def.name;
    this.removeModelBtn.classList.toggle('hidden', !model.isUserModel);
    this.setStatus(`Active: ${model.def.name}`);
  }

  setStatus(msg) {
    this.statusText.textContent = msg;
  }

  animate() {
    requestAnimationFrame(() => this.animate());

    // Smoothly morph current particle positions & colors toward target buffers
    const posAttr = this.geometry.attributes.position;
    const colAttr = this.geometry.attributes.color;
    const lerpSpeed = 0.08;

    for (let i = 0; i < this.currentPositions.length; i++) {
      this.currentPositions[i] += (this.targetPositions[i] - this.currentPositions[i]) * lerpSpeed;
      this.currentColors[i] += (this.targetColors[i] - this.currentColors[i]) * lerpSpeed;
    }

    posAttr.needsUpdate = true;
    colAttr.needsUpdate = true;

    this.points.rotation.y += 0.004;
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }
}

window.addEventListener('DOMContentLoaded', () => {
  new HoloMotionParticleApp();
});
