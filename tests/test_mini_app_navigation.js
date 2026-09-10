"use strict";

const assert = require("node:assert/strict");
const { ResourceStore } = require("../bot/miniapp/resource_store.js");

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

class FakeElement {
  constructor(dataset = {}) {
    this.dataset = dataset;
    this.listeners = {};
    this.classNames = new Set();
    this.classList = {
      add: (...names) => names.forEach((name) => this.classNames.add(name)),
      remove: (...names) => names.forEach((name) => this.classNames.delete(name)),
      toggle: (name, active) => {
        if (active) this.classNames.add(name);
        else this.classNames.delete(name);
      },
    };
    this.innerHTML = "";
    this.textContent = "";
  }

  addEventListener(name, listener) {
    this.listeners[name] = listener;
  }

  setAttribute() {}
  querySelector() { return null; }
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
}

async function run() {
  const ids = [
    "view", "loading-screen", "error-screen", "error-message", "page-title",
    "date-label", "refresh-button", "retry-button", "add-task-button",
    "task-dialog", "task-form", "toast",
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new FakeElement()]));
  const navItems = ["today", "planner", "tasks", "health", "profile"].map(
    (view) => new FakeElement({ view }),
  );
  const requests = new Map();

  global.window = {
    MiniAppResourceStore: ResourceStore,
    scrollTo() {},
  };
  global.location = { search: "" };
  global.document = {
    visibilityState: "visible",
    addEventListener() {},
    querySelector(selector) {
      if (selector.startsWith("#")) return elements[selector.slice(1)] || null;
      return null;
    },
    querySelectorAll(selector) {
      return selector === ".nav-item" ? navItems : [];
    },
  };
  global.fetch = (path) => {
    const request = deferred();
    requests.set(path, request);
    return request.promise;
  };

  require("../bot/miniapp/app.js");
  await flush();
  assert.ok(requests.has("api/v1/state?period=today"));

  navItems.find((item) => item.dataset.view === "profile").listeners.click();
  await flush();
  assert.equal(elements["page-title"].textContent, "Профиль");
  assert.ok(requests.has("api/v1/profile"));

  requests.get("api/v1/profile").resolve({
    ok: true,
    status: 200,
    json: async () => ({
      name: "Тест",
      timezone: "Europe/Moscow",
      timezone_options: [{ timezone: "Europe/Moscow", label: "Москва", offset: "+03:00" }],
      home: "",
      planning: {
        daily_focus_minutes: 180,
        auto_planning_minutes: 180,
        focus_load_level: "balanced",
        focus_warning: "",
      },
      hse: {
        events: 0,
        synced_at: null,
        sync_status: "never",
        last_result: null,
        iphone_bridge: false,
        first_start: null,
        last_end: null,
      },
    }),
  });
  await flush();
  assert.match(elements.view.innerHTML, /Подключения · календарь HSE/);
  assert.match(elements.view.innerHTML, /Ещё не запускалась/);

  navItems.find((item) => item.dataset.view === "health").listeners.click();
  await flush();
  assert.equal(elements["page-title"].textContent, "Здоровье");
  assert.ok(requests.has("api/v1/health"));
  requests.get("api/v1/health").resolve({
    ok: true,
    status: 200,
    json: async () => ({
      now: "2026-09-09T10:00:00+03:00",
      timezone: "Europe/Moscow",
      apple_health: { connected: false, bridge_enabled: true, snapshot: null },
      recovery: {
        level: "unknown",
        title: "Нет данных Apple Health",
        message: "Подключи сон и шаги.",
        recommendations: [],
      },
      intake: { water_ml: 0, protein_g: 0, calories_kcal: 0, fat_g: 0, carbs_g: 0 },
      goals: { water_ml: 0, protein_g: 0 },
      training: {
        configured: false,
        goal: "",
        upcoming: [],
        history: [],
        micro_plan: { level: "setup", title: "Сначала задай цель", text: "Напиши боту." },
      },
      medical_disclaimer: "Не медицинская диагностика.",
    }),
  });
  await flush();
  assert.match(elements.view.innerHTML, /Баланс дня/);
  assert.match(elements.view.innerHTML, /Подключить Apple Health/);
  assert.equal(elements["add-task-button"].classNames.has("hidden"), true);

  requests.get("api/v1/state?period=today").resolve({
    ok: true,
    status: 200,
    json: async () => ({
      period: "today",
      now: "2026-09-09T10:00:00+03:00",
      user: { timezone: "Europe/Moscow" },
      stats: { events: 0, active_tasks: 0, overdue_tasks: 0 },
      timeline: [],
      tasks: [],
      focus: [],
      conflicts: [],
      upcoming: {
        next_hse_event: {
          title: "Будущая лекция",
          start: "2026-09-23T16:20:00+03:00",
          end: "2026-09-23T17:40:00+03:00",
        },
      },
      sources: {
        hse: {
          status: "incomplete",
          message: "Телефон прислал неполный снимок.",
        },
      },
      assistant: {
        now: null,
        next: null,
        risks: [{
          level: "warning",
          title: "Расписание HSE неполное",
          text: "Телефон прислал неполный снимок.",
        }],
        resource: {
          connected: false,
          level: "unknown",
          title: "Нет данных о восстановлении",
        },
      },
    }),
  });
  await flush();
  navItems.find((item) => item.dataset.view === "today").listeners.click();
  assert.equal(elements["page-title"].textContent, "Сегодня");
  assert.match(elements.view.innerHTML, /Здесь свободно/);

  navItems.find((item) => item.dataset.view === "planner").listeners.click();
  assert.equal(elements["page-title"].textContent, "Планнер");
  assert.match(elements.view.innerHTML, /Из неполного снимка известно событие/);
  assert.match(elements.view.innerHTML, /Расписание HSE неполное/);
  assert.match(elements.view.innerHTML, /Будущая лекция|23 сентября/);

  console.log("Mini App independent navigation checks passed.");
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
