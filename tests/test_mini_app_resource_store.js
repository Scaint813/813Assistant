"use strict";

const assert = require("node:assert/strict");
const { ResourceStore } = require("../bot/miniapp/resource_store.js");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function nextMicrotask() {
  await Promise.resolve();
}

async function run() {
  const requests = new Map();
  const loader = (key) => {
    const request = deferred();
    const queue = requests.get(key) || [];
    queue.push(request);
    requests.set(key, queue);
    return request.promise;
  };
  const store = new ResourceStore(loader);

  const todayPromise = store.load("schedule:today");
  const profilePromise = store.load("profile");
  await nextMicrotask();
  assert.equal(store.get("schedule:today").loading, true);
  assert.equal(store.get("profile").loading, true);

  requests.get("profile")[0].resolve({ name: "Профиль" });
  await profilePromise;
  assert.equal(store.get("profile").data.name, "Профиль");
  assert.equal(store.get("schedule:today").loading, true);

  requests.get("schedule:today")[0].resolve({ timeline: [] });
  await todayPromise;
  assert.deepEqual(store.get("schedule:today").data.timeline, []);
  await store.load("schedule:today");
  assert.equal(requests.get("schedule:today").length, 1);

  const failedPromise = store.load("schedule:tomorrow");
  await nextMicrotask();
  requests.get("schedule:tomorrow")[0].reject(new Error("tomorrow failed"));
  await assert.rejects(failedPromise, /tomorrow failed/);
  assert.match(store.get("schedule:tomorrow").error.message, /tomorrow failed/);
  assert.deepEqual(store.get("schedule:today").data.timeline, []);

  const oldWeekPromise = store.load("schedule:week");
  await nextMicrotask();
  const newWeekPromise = store.load("schedule:week", true);
  await nextMicrotask();
  requests.get("schedule:week")[1].resolve({ version: "new" });
  await newWeekPromise;
  requests.get("schedule:week")[0].resolve({ version: "old" });
  await oldWeekPromise;
  assert.equal(store.get("schedule:week").data.version, "new");

  console.log("Mini App resource store checks passed.");
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
