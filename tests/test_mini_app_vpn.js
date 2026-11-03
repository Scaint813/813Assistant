"use strict";

const assert = require("node:assert/strict");

function snapshot() {
  const checkedAt = "2026-11-03T12:00:00+00:00";
  return {
    enabled: true,
    checked_at: checkedAt,
    exit_node: {
      name: "economic-peach",
      expected_ipv4: "89.22.228.86",
      expected_ipv6: "2a0b:4140:5020::2",
      country_code: "FI",
      country_source: "configured",
      static_ip_verified: null,
    },
    server: { state: "running", online: true, exit_node_available: true, checked_at: checkedAt },
    devices: [{ name: "Mac", tailscale_ips: ["100.124.44.54"], online: true, last_seen: null, exit_node_in_use: null }],
    connection: {
      state: "via_vps",
      observed_ip: "89.22.228.86",
      family: 4,
      source: "proxy",
      checked_at: checkedAt,
      unverified_families: [6],
    },
  };
}

function profile(owner = true) {
  return {
    name: "Владелец",
    timezone: "Europe/Moscow",
    timezone_options: [],
    planning: { daily_focus_minutes: 180, auto_planning_minutes: 180 },
    hse: {},
    vpn_panel_available: owner,
  };
}

function entry(data = snapshot(), options = {}) {
  return { data, error: null, loading: false, ...options };
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

function response(data, status = 200) {
  return { ok: status < 400, status, json: async () => data };
}

class FakeElement {
  constructor(dataset = {}) {
    this.dataset = dataset;
    this.listeners = {};
    this.classNames = new Set();
    this.classList = {
      add: (...names) => names.forEach((name) => this.classNames.add(name)),
      remove: (...names) => names.forEach((name) => this.classNames.delete(name)),
      toggle: (name, active) => active ? this.classNames.add(name) : this.classNames.delete(name),
    };
    this.innerHTML = "";
    this.textContent = "";
  }

  addEventListener(name, listener) { this.listeners[name] = listener; }
  setAttribute() {}
  querySelector() { return null; }
  close() {}
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
}

async function run() {
  const { createViewRenderer } = await import("../bot/miniapp/views.mjs?v=3");
  const renderer = createViewRenderer({ getTimezone: () => "Europe/Moscow", periods: {} });

  assert.match(renderer.renderProfile(profile()), /data-nav-view="vpn"/);
  assert.doesNotMatch(renderer.renderProfile(profile(false)), /data-nav-view="vpn"/);
  const ready = renderer.renderVpn(entry());
  assert.match(ready, /vpn-route-card good/);
  assert.match(ready, /Этот запрос идёт через VPS/);
  assert.match(ready, /На телефоне она не проверяет маршрут Mac/);
  assert.match(ready, /IPv6 этим запросом не проверен/);
  assert.match(ready, /Финляндия \(FI\)/);
  assert.match(ready, /Закрепление IP у хостера не проверено/);
  assert.match(ready, /Использование Exit Node на устройстве не проверено/);
  assert.doesNotMatch(ready, /data-vpn-toggle|VPN работает/);

  const unknownRoute = snapshot();
  unknownRoute.connection = { state: "unknown", observed_ip: null, family: null };
  assert.doesNotMatch(renderer.renderVpn(entry(unknownRoute)), /vpn-route-card good/);
  assert.match(renderer.renderVpn(entry(unknownRoute)), /Маршрут не подтверждён/);

  const differentNode = snapshot();
  differentNode.server = {
    state: "different_node", name: "different-host", matches_expected: false,
    online: null, exit_node_available: null,
  };
  const differentNodeHTML = renderer.renderVpn(entry(differentNode));
  assert.match(differentNodeHTML, /Проверяется другой узел/);
  assert.match(differentNodeHTML, /different-host/);
  assert.match(differentNodeHTML, /Состояние выбранного VPS неизвестно/);

  const ipv6 = snapshot();
  ipv6.connection.family = 6;
  ipv6.connection.observed_ip = ipv6.exit_node.expected_ipv6;
  assert.match(renderer.renderVpn(entry(ipv6)), /IPv4 этим запросом не проверен/);

  for (const options of [
    { loading: true },
    { error: { status: 503 } },
  ]) {
    const html = renderer.renderVpn(entry(snapshot(), options));
    assert.doesNotMatch(html, /vpn-route-card good/);
    assert.match(html, /IP последней проверки/);
    assert.match(html, /Текущее состояние не подтверждено/);
  }
  assert.doesNotMatch(renderer.renderVpn(entry(), { needsCheck: true }), /vpn-route-card good/);
  assert.match(renderer.renderVpn(entry({ ...snapshot(), enabled: false })), /Панель VPN ещё не настроена/);
  for (const status of [401, 403]) {
    const html = renderer.renderVpn(entry(snapshot(), { error: { status } }));
    assert.doesNotMatch(html, /89\.22\.228\.86|economic-peach|100\.124\.44\.54/);
  }
  const malicious = snapshot();
  malicious.exit_node.name = '<img src=x onerror="alert(1)">';
  malicious.exit_node.expected_ipv4 = '<script>alert(2)</script>';
  malicious.devices[0].name = '<svg onload="alert(3)">';
  malicious.connection.observed_ip = '<a href="javascript:alert(4)">';
  malicious.connection.checked_at = "invalid timestamp";
  const escaped = renderer.renderVpn(entry(malicious));
  assert.doesNotMatch(escaped, /<img|<script|<svg|href="javascript:/);
  assert.match(escaped, /&lt;img/);

  const ids = [
    "view", "loading-screen", "error-screen", "error-message", "page-title",
    "date-label", "refresh-button", "retry-button", "add-task-button",
    "help-button", "task-dialog", "task-form", "setup-dialog", "setup-content",
    "toast", "offline-banner",
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new FakeElement()]));
  const navItems = ["today", "planner", "tasks", "health", "profile"].map(
    (view) => new FakeElement({ view }),
  );
  const listeners = {};
  const timers = [];
  const requests = [];
  let telegramBack;
  let backVisible = false;
  global.window = {
    location: { pathname: "/assistant/", search: "", hash: "" },
    history: { pushState() {}, replaceState() {} },
    sessionStorage: { getItem() { return null; }, setItem() {} },
    addEventListener(name, callback) { listeners[`window:${name}`] = callback; },
    scrollTo() {},
    Telegram: { WebApp: {
      initData: "signed-telegram-init-data",
      ready() {}, expand() {},
      BackButton: {
        show() { backVisible = true; },
        hide() { backVisible = false; },
        onClick(callback) { telegramBack = callback; },
      },
    } },
  };
  Object.defineProperty(global, "navigator", { configurable: true, value: { onLine: true } });
  global.document = {
    visibilityState: "visible",
    addEventListener(name, callback) { listeners[name] = callback; },
    querySelector(selector) { return selector.startsWith("#") ? elements[selector.slice(1)] || null : null; },
    querySelectorAll(selector) { return selector === ".nav-item" ? navItems : []; },
  };
  global.setInterval = (callback, milliseconds) => {
    timers.push({ callback, milliseconds });
    return { unref() {} };
  };
  global.fetch = (path, options) => {
    if (path.startsWith("api/v1/state")) {
      return Promise.resolve(response({ now: "2026-11-03T12:00:00Z", user: {}, timeline: [] }));
    }
    const request = { path, options, ...deferred() };
    requests.push(request);
    return request.promise;
  };
  const requestCount = () => requests.filter((request) => request.path === "api/v1/vpn").length;
  const latestRequest = () => requests[requests.length - 1];
  const click = (dataset = {}, attributes = []) => listeners.click({ target: {
    closest: () => ({ dataset, hasAttribute: (name) => attributes.includes(name) }),
  } });

  await import("../bot/miniapp/app.mjs?v=vpn-tests");
  await flush();
  navItems.find((item) => item.dataset.view === "profile").listeners.click();
  await flush();
  latestRequest().resolve(response(profile()));
  await flush();
  assert.match(elements.view.innerHTML, /data-nav-view="vpn"/);

  click({ navView: "vpn" });
  await flush();
  assert.equal(elements["page-title"].textContent, "Мой VPN");
  assert.equal(requestCount(), 1);
  assert.equal(latestRequest().options.headers.Authorization, "tma signed-telegram-init-data");
  assert.equal(backVisible, true);
  assert.equal(navItems.find((item) => item.dataset.view === "profile").classNames.has("active"), true);
  assert.equal(navItems.length, 5);
  latestRequest().resolve(response(snapshot()));
  await flush();
  assert.match(elements.view.innerHTML, /vpn-route-card good/);

  elements["refresh-button"].listeners.click();
  await flush();
  assert.equal(requestCount(), 2);
  assert.doesNotMatch(elements.view.innerHTML, /vpn-route-card good/);
  assert.match(elements.view.innerHTML, /Проверяем подключение/);
  latestRequest().resolve(response({ error: "probe unavailable" }, 503));
  await flush();
  assert.match(elements.view.innerHTML, /Не удалось обновить проверку/);
  assert.doesNotMatch(elements.view.innerHTML, /vpn-route-card good/);

  click({}, ["data-retry-current"]);
  await flush();
  latestRequest().resolve(response(snapshot()));
  await flush();
  assert.match(elements.view.innerHTML, /vpn-route-card good/);

  document.visibilityState = "hidden";
  listeners.visibilitychange();
  assert.doesNotMatch(elements.view.innerHTML, /vpn-route-card good/);
  document.visibilityState = "visible";
  listeners.visibilitychange();
  await flush();
  assert.equal(requestCount(), 4);
  assert.doesNotMatch(elements.view.innerHTML, /vpn-route-card good/);
  latestRequest().resolve(response(snapshot()));
  await flush();

  const originalNow = Date.now;
  try {
    const future = originalNow() + 46_000;
    Date.now = () => future;
    timers.find((timer) => timer.milliseconds === 5_000).callback();
    assert.doesNotMatch(elements.view.innerHTML, /vpn-route-card good/);
    assert.match(elements.view.innerHTML, /Результат мог измениться/);
  } finally {
    Date.now = originalNow;
  }

  telegramBack();
  assert.equal(elements["page-title"].textContent, "Профиль");
  assert.equal(backVisible, false);
  click({ navView: "vpn" });
  await flush();
  assert.equal(requestCount(), 5);
  assert.doesNotMatch(elements.view.innerHTML, /vpn-route-card good/);
  latestRequest().resolve(response(snapshot()));
  await flush();

  elements["help-button"].listeners.click();
  assert.equal(elements["page-title"].textContent, "Справка");
  telegramBack();
  await flush();
  assert.equal(elements["page-title"].textContent, "Мой VPN");
  latestRequest().resolve(response({ error: "owner only" }, 403));
  await flush();
  assert.match(elements.view.innerHTML, /Панель доступна владельцу/);
  assert.doesNotMatch(elements.view.innerHTML, /economic-peach|89\.22\.228\.86/);

  elements["refresh-button"].listeners.click();
  await flush();
  assert.match(elements.view.innerHTML, /Панель доступна владельцу/);
  assert.doesNotMatch(elements.view.innerHTML, /economic-peach|89\.22\.228\.86/);
  latestRequest().resolve(response({ error: "still unavailable" }, 503));
  await flush();
  assert.doesNotMatch(elements.view.innerHTML, /economic-peach|89\.22\.228\.86/);

  elements["refresh-button"].listeners.click();
  await flush();
  assert.doesNotMatch(elements.view.innerHTML, /economic-peach|89\.22\.228\.86/);
  latestRequest().resolve(response(snapshot()));
  await flush();
  assert.match(elements.view.innerHTML, /vpn-route-card good/);
  telegramBack();
  assert.equal(elements["page-title"].textContent, "Профиль");
  console.log("Mini App VPN rendering, ownership and fresh-request navigation checks passed.");
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
