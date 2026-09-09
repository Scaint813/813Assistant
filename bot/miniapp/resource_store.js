(function exposeResourceStore(root, factory) {
  "use strict";

  const ResourceStore = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = { ResourceStore };
  }
  if (root) root.MiniAppResourceStore = ResourceStore;
})(typeof window === "undefined" ? null : window, function createResourceStore() {
  "use strict";

  class ResourceStore {
    constructor(loader, onChange) {
      this.loader = loader;
      this.onChange = onChange || function noop() {};
      this.entries = new Map();
    }

    get(key) {
      if (!this.entries.has(key)) {
        this.entries.set(key, {
          data: null,
          error: null,
          loading: false,
          promise: null,
          version: 0,
        });
      }
      return this.entries.get(key);
    }

    load(key, force) {
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
          if (entry.version === requestVersion) entry.data = data;
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
      this.onChange(key, entry);
    }

    invalidate(key) {
      const entry = this.get(key);
      entry.version += 1;
      entry.data = null;
      entry.error = null;
      entry.loading = false;
      entry.promise = null;
      this.onChange(key, entry);
    }

    invalidatePrefix(prefix) {
      this.entries.forEach((_entry, key) => {
        if (key.startsWith(prefix)) this.invalidate(key);
      });
    }
  }

  return ResourceStore;
});
