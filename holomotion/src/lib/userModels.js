const DB_NAME = 'holomotion_particle_models_db';
const STORE_NAME = 'custom_models';
const DB_VERSION = 1;

function openDatabase() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);

    request.onupgradeneeded = (event) => {
      const db = event.target.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: 'id' });
      }
    };

    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

/**
 * Persists a sampled particle model entry in IndexedDB so custom uploads survive reloads.
 */
export async function saveUserParticleModel(modelObj) {
  const db = await openDatabase();
  const id = `custom-${Date.now()}`;
  const record = {
    id,
    isUserModel: true,
    def: modelObj.def,
    n: modelObj.n,
    target: modelObj.target,
    offset: modelObj.offset,
    tint: modelObj.tint,
    pick: modelObj.pick,
    labels: modelObj.labels,
    groups: modelObj.groups,
    fitScale: modelObj.fitScale,
    exCenter: modelObj.exCenter,
    ms: modelObj.ms
  };

  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite');
    tx.objectStore(STORE_NAME).put(record);
    tx.oncomplete = () => resolve(record);
    tx.onerror = () => reject(tx.error);
  });
}

/**
 * Loads all saved custom particle models from IndexedDB.
 */
export async function getUserParticleModels() {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readonly');
    const req = tx.objectStore(STORE_NAME).getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

/**
 * Deletes a saved custom particle model by ID.
 */
export async function deleteUserParticleModel(id) {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, 'readwrite');
    tx.objectStore(STORE_NAME).delete(id);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}
