// Remembers the models the user added (the original files) in the browser's IndexedDB so they come back after a reload.
const DB = 'holomotion-user-models', STORE = 'files';
const open = () => new Promise((res, rej) => {
  const r = indexedDB.open(DB, 1);
  r.onupgradeneeded = () => r.result.createObjectStore(STORE, { keyPath: 'id', autoIncrement: true });
  r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
});
const tx = async (mode, fn) => {
  const db = await open();
  return new Promise((res, rej) => { const t = db.transaction(STORE, mode), q = fn(t.objectStore(STORE)); t.oncomplete = () => res(q && q.result); t.onerror = () => rej(t.error); });
};
export const saveUserFile = (name, buffer) => tx('readwrite', (s) => s.add({ name, buffer, added: Date.now() }));
export const loadUserFiles = () => tx('readonly', (s) => s.getAll());
export const clearUserFiles = () => tx('readwrite', (s) => s.clear());
