export class ResourceStore {
  constructor(loader, onChange = () => {}) {
    this.loader = loader;
    this.onChange = onChange;
    this.entries = new Map();
  }

  get(key) {
    if (!this.entries.has(key)) {
      this.entries.set(key, {
        data: null,
        error: null,
        loading: false,
        promise: null,
        updatedAt: 0,
        version: 0,
      });
    }
    return this.entries.get(key);
  }

  load(key, force = false) {
    const entry = this.get(key);
    if (!force && entry.data !== null) return Promise.resolve(entry.data);
    if (entry.promise && !force) return entry.promise;

    const requestVersion = entry.version + 1;
    entry.version = requestVersion;
    entry.loading = true;
    entry.error = null;
    this.onChange(key, entry);

    const request = Promise.resolve()
      .then(() => this.loader(key))
      .then((data) => {
        if (entry.version === requestVersion) {
          entry.data = data;
          entry.updatedAt = Date.now();
        }
        return data;
      })
      .catch((error) => {
        if (entry.version === requestVersion) entry.error = error;
        throw error;
      })
      .finally(() => {
        if (entry.promise === request) {
          entry.loading = false;
          entry.promise = null;
          this.onChange(key, entry);
        }
      });
    entry.promise = request;
    return request;
  }

  set(key, data) {
    const entry = this.get(key);
    entry.version += 1;
    entry.data = data;
    entry.error = null;
    entry.loading = false;
    entry.promise = null;
    entry.updatedAt = Date.now();
    this.onChange(key, entry);
  }

  invalidate(key) {
    const entry = this.get(key);
    entry.version += 1;
    entry.data = null;
    entry.error = null;
    entry.loading = false;
    entry.promise = null;
    entry.updatedAt = 0;
    this.onChange(key, entry);
  }

  invalidatePrefix(prefix) {
    this.entries.forEach((_entry, key) => {
      if (key.startsWith(prefix)) this.invalidate(key);
    });
  }
}
