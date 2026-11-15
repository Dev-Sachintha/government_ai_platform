// Robust webcam + MediaPipe HandLandmarker (Tasks Vision API).
// Uses lower confidence thresholds to help webcams with dim/noisy images.
// Loaded from a CDN so no npm install / build step is needed (pinned version).
import { FilesetResolver, HandLandmarker } from 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs';

const WASM = 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm';
const MODEL = new URL('../models/hand_landmarker.task', import.meta.url).href;

export class HandCamera {
  constructor(video) {
    this.video = video;
    this.landmarker = null;
    this.stream = null;
    this.running = false;
    this.delegate = null;
    this.lastVideoTime = -1;
    this.lastTs = 0;
    this.frames = 0;
    this.fps = 0;
    this.detectMs = 0;
    this._n = 0;
    this._t = performance.now();
    this.newFrame = false;
  }

  async start(onStatus = () => {}) {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw new Error('Camera access is unavailable. Open this app using http://localhost or HTTPS.');
    }
    onStatus('Requesting camera permission…');
    this.stop();

    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        facingMode: { ideal: 'user' },
        width: { ideal: 640 },
        height: { ideal: 480 },
        frameRate: { ideal: 30, max: 30 },
      },
    });
    this.video.srcObject = this.stream;
    this.video.autoplay = true;
    this.video.muted = true;
    this.video.playsInline = true;

    // Wait for actual image dimensions before creating the detector.
    if (this.video.readyState < 1 || !this.video.videoWidth) {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error('Timed out waiting for webcam video. Close other camera apps and try again.')), 10000);
        const cleanup = () => {
          clearTimeout(timer);
          this.video.removeEventListener('loadedmetadata', loaded);
          this.video.removeEventListener('error', failed);
        };
        const loaded = () => { cleanup(); resolve(); };
        const failed = () => { cleanup(); reject(new Error('The webcam stream could not provide video frames.')); };
        this.video.addEventListener('loadedmetadata', loaded, { once: true });
        this.video.addEventListener('error', failed, { once: true });
      });
    }
    await this.video.play();
    if (!this.video.videoWidth || !this.video.videoHeight) {
      throw new Error('Camera opened but video dimensions are zero. Check Windows camera privacy settings.');
    }

    onStatus('Loading hand detection model…');
    const fileset = await FilesetResolver.forVisionTasks(WASM);
    const make = (delegate) => HandLandmarker.createFromOptions(fileset, {
      baseOptions: { modelAssetPath: MODEL, delegate },
      runningMode: 'VIDEO',
      numHands: 2,
      minHandDetectionConfidence: 0.35,
      minHandPresenceConfidence: 0.35,
      minTrackingConfidence: 0.35,
    });
    try {
      this.landmarker = await make('GPU');
      this.delegate = 'GPU';
    } catch (gpuError) {
      console.warn('MediaPipe GPU initialization failed; using CPU.', gpuError);
      this.landmarker = await make('CPU');
      this.delegate = 'CPU';
    }
    this.lastVideoTime = -1;
    this.lastTs = 0;
    this.frames = 0;
    this._n = 0;
    this._t = performance.now();
    this.running = true;
    onStatus(`Camera ready (${this.video.videoWidth}×${this.video.videoHeight}); show one full hand to the camera.`);
  }

  // Returns undefined if no new frame arrived; otherwise returns zero, one or two landmark arrays.
  poll() {
    const v = this.video;
    if (!this.running || !this.landmarker || v.readyState < 2 || !v.videoWidth || v.currentTime === this.lastVideoTime) return undefined;
    this.lastVideoTime = v.currentTime;
    this.newFrame = true;
    const ts = Math.max(Math.round(performance.now()), this.lastTs + 1);
    this.lastTs = ts;
    const t0 = performance.now();
    try {
      const result = this.landmarker.detectForVideo(v, ts);
      this.detectMs = performance.now() - t0;
      this.frames++;
      this._n++;
      const now = performance.now();
      if (now - this._t >= 1000) {
        this.fps = this._n * 1000 / (now - this._t);
        this._n = 0;
        this._t = now;
      }
      // App's UI is mirrored like a selfie camera; the controller expects [x,y] pairs.
      return (result.landmarks || []).map((landmarks) => landmarks.map((p) => [1 - p.x, p.y]));
    } catch (error) {
      console.error('MediaPipe hand detection failed:', error);
      this.running = false;
      throw error;
    }
  }

  stop() {
    this.running = false;
    if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
    this.stream = null;
    if (this.video) this.video.srcObject = null;
    this.landmarker?.close?.();
    this.landmarker = null;
    this.newFrame = false;
  }
}
