"use strict";

const assert = require("node:assert/strict");

function snapshot() {
  const checkedAt = "2026-11-03T12:00:00+00:00";
  return {
    enabled: true, checked_at: checkedAt,
    exit_node: {
      name: "economic-peach", expected_ipv4: "89.22.228.86", expected_ipv6: "2a0b:4140:5020::2",
      country_code: "FI", country_source: "configured", static_ip_verified: null,
    },
    server: { state: "running", online: true, exit_node_available: true, checked_at: checkedAt },
    devices: [{ name: "Mac", tailscale_ips: ["100.124.44.54"], online: true, last_seen: null, exit_node_in_use: null }],
    connection: {
      state: "via_tailnet", observed_ip: "100.124.44.54", family: 4,
      source: "proxy", checked_at: checkedAt, unverified_families: [4, 6],
    },
  };
}

function profile(owner = true) {
  return {
    name: "Владелец", timezone: "Europe/Moscow", timezone_options: [],
    planning: { daily_focus_minutes: 180, auto_planning_minutes: 180 },
    hse: {}, vpn_panel_available: owner,
  };
}

function entry(data = snapshot(), options = {}) {
  return { data, error: null, loading: false, ...options };
}

function externalResult(family, observedIp, error = null, options = {}) {
  return {
    family, observed_ip: observedIp, error, status: "complete", updatedAt: Date.now(),
    checked_at: new Date().toISOString(), provider: family === 4 ? "api.ipify.org" : "api6.ipify.org",
    ...options,
  };
}

function familyHTML(html, family) {
  return html.match(new RegExp(`<article class="vpn-external-family [^"]*" data-vpn-external-family="${family}">[\\s\\S]*?</article>`))?.[0] || "";
}

function deviceHTML(html, name = "Mac") {
  const row = (html.match(/<li class="vpn-device">[\s\S]*?<\/li>/g) || [])
    .find((value) => value.includes(`<strong>${name}</strong>`));
  assert.ok(row, `device row for ${name} exists`);
  return row;
}

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

function response(data, status = 200) {
  return new Response(JSON.stringify(data), { status });
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

async function flush() { await new Promise((resolve) => setImmediate(resolve)); }

async function run() {
  const { createViewRenderer } = await import("../bot/miniapp/views.mjs?v=6");
  const renderer = createViewRenderer({ getTimezone: () => "Europe/Moscow", periods: {} });
  assert.match(renderer.renderProfile(profile()), /data-nav-view="vpn"/);
  assert.doesNotMatch(renderer.renderProfile(profile(false)), /data-nav-view="vpn"/);
  const ready = renderer.renderVpn(entry());
  for (const pattern of [
    /vpn-route-card neutral/, /Подключение к VPS по Tailscale/, /внутренний адрес устройства в Tailscale/,
    /Внешние IPv4 и IPv6 этим запросом не проверены/, /На телефоне она не проверяет маршрут Mac/,
    /Финляндия \(FI\)/, /Закрепление IP у хостера не проверено/,
    /Выбор Exit Node в системе удалённо не проверяется/, /Обновить подключение/, /Внешний IP этого устройства/,
    /По нажатию запросы к api.ipify.org и api6.ipify.org; сервисы увидят ваш IP. Telegram-авторизация не передаётся/,
  ]) assert.match(ready, pattern);
  assert.doesNotMatch(ready, /data-vpn-toggle|VPN работает|Этот запрос идёт через VPS|Совпадает с VPS/);
  for (const family of [4, 6]) assert.match(familyHTML(ready, family), /Не проверен/);
  assert.match(deviceHTML(ready), /На этом устройстве открыт миниапп/);
  for (const family of [4, 6]) assert.match(deviceHTML(ready), new RegExp(`IPv${family} миниаппа: Не проверен`));

  const online = snapshot();
  online.server.checked_at = "2026-11-03T10:11:00Z";
  online.checked_at = "2026-11-03T12:22:00Z";
  online.connection.checked_at = "2026-11-03T13:33:00Z";
  const onlineRow = deviceHTML(renderer.renderVpn(entry(online)));
  assert.match(onlineRow, /Доступность проверена: [^<]*13:11/);
  assert.doesNotMatch(onlineRow, /Последний контакт|15:22|16:33/);
  online.devices[0].last_seen = "2026-11-01T08:17:00Z";
  const onlineWithLastSeenRow = deviceHTML(renderer.renderVpn(entry(online)));
  assert.match(onlineWithLastSeenRow, /Доступность проверена: [^<]*13:11/);
  assert.doesNotMatch(onlineWithLastSeenRow, /Последний контакт|11:17/);
  const offline = snapshot();
  offline.devices[0].online = false;
  offline.devices[0].last_seen = "2026-11-03T08:17:00Z";
  const offlineRow = deviceHTML(renderer.renderVpn(entry(offline)));
  assert.match(offlineRow, /Не в сети/);
  assert.match(offlineRow, /Последний контакт: [^<]*11:17/);
  assert.doesNotMatch(offlineRow, /Доступность проверена/);
  const unknownDevice = snapshot();
  unknownDevice.devices[0].online = null;
  assert.match(deviceHTML(renderer.renderVpn(entry(unknownDevice))), /Нет данных|нет данных/);
  const missingProbeTime = snapshot();
  missingProbeTime.server.checked_at = null;
  const missingProbeTimeRow = deviceHTML(renderer.renderVpn(entry(missingProbeTime)));
  assert.match(missingProbeTimeRow, /Нет данных|нет данных|неизвестн|не получен|недоступно/);
  assert.doesNotMatch(missingProbeTimeRow, /15:00/);

  const external = { 4: externalResult(4, "89.22.228.86"), 6: externalResult(6, null, "unavailable") };
  const partial = renderer.renderVpn(entry(), { external, externalCheckEnabled: true });
  assert.match(familyHTML(partial, 4), /vpn-external-family good/);
  assert.match(familyHTML(partial, 4), /Совпадает с VPS/);
  assert.match(familyHTML(partial, 6), /Проверка недоступна/);
  assert.doesNotMatch(familyHTML(partial, 6), /good|Другой адрес/);
  assert.match(deviceHTML(partial), /IPv4 миниаппа: Совпадает с VPS/);
  assert.match(deviceHTML(partial), /IPv6 миниаппа: Проверка недоступна/);
  assert.match(deviceHTML(partial), /Выбор Exit Node в системе удалённо не проверяется/);
  const twoDevices = snapshot();
  twoDevices.devices.push({ name: "Phone", tailscale_ips: ["100.65.1.2"], online: true, last_seen: null, exit_node_in_use: null });
  const twoDevicesHTML = renderer.renderVpn(entry(twoDevices), { external });
  assert.match(deviceHTML(twoDevicesHTML), /На этом устройстве открыт миниапп/);
  assert.match(deviceHTML(twoDevicesHTML), /IPv4 миниаппа: Совпадает с VPS/);
  assert.doesNotMatch(deviceHTML(twoDevicesHTML, "Phone"), /На этом устройстве открыт миниапп|IPv[46] миниаппа|Совпадает с VPS/);
  for (const connection of [
    { state: "via_tailnet", observed_ip: "100.124.44.540", family: 4 },
    { state: "via_tailnet", observed_ip: "100.65.1.3", family: 4 },
    { state: "via_vps", observed_ip: "89.22.228.86", family: 4 },
    { state: "other_route", observed_ip: "100.124.44.54", family: 4 },
    { state: "unknown", observed_ip: "100.124.44.54", family: 4 },
  ]) {
    const unmatched = { ...snapshot(), connection };
    assert.doesNotMatch(deviceHTML(renderer.renderVpn(entry(unmatched), { external })), /На этом устройстве открыт миниапп|IPv[46] миниаппа|Совпадает с VPS/);
  }
  const ipv6Device = snapshot();
  ipv6Device.devices[0].tailscale_ips.push("fd7a:115c:a1e0::a1");
  ipv6Device.connection = { state: "via_tailnet", observed_ip: "fd7a:115c:a1e0::a1", family: 6 };
  assert.match(deviceHTML(renderer.renderVpn(entry(ipv6Device), { external })), /На этом устройстве открыт миниапп/);
  for (const [resourceOptions, renderOptions] of [
    [{ loading: true }, {}], [{ error: { status: 503 } }, {}], [{}, { needsCheck: true }],
  ]) {
    const staleDevice = deviceHTML(renderer.renderVpn(entry(snapshot(), resourceOptions), { external, ...renderOptions }));
    assert.match(staleDevice, /Нет свежих данных/);
    assert.doesNotMatch(staleDevice, /На этом устройстве открыт миниапп|IPv[46] миниаппа: Совпадает с VPS/);
  }
  const canonical = snapshot();
  canonical.exit_node.expected_ipv6 = "2A0B:4140:5020:0:0:0:0:2";
  assert.match(familyHTML(renderer.renderVpn(entry(canonical), {
    external: { 6: externalResult(6, "2a0b:4140:5020::2") },
  }), 6), /Совпадает с VPS/);
  const missing = snapshot();
  missing.exit_node.expected_ipv6 = null;
  const noExpected = familyHTML(renderer.renderVpn(entry(missing), {
    external: { 6: externalResult(6, "2a0b:4140:5020::2") },
  }), 6);
  assert.match(noExpected, /Ожидаемый адрес не настроен/);
  assert.doesNotMatch(noExpected, /Другой адрес|good/);
  assert.match(familyHTML(renderer.renderVpn(entry(), {
    external: { 4: externalResult(4, "1.1.1.1") },
  }), 4), /Другой адрес/);
  for (const result of [
    externalResult(4, "89.22.228.86", null, { status: "stale" }),
    externalResult(4, "89.22.228.86", null, { updatedAt: Date.now() - 46_000 }),
    externalResult(4, "89.22.228.86", null, { updatedAt: undefined }),
    externalResult(4, "89.22.228.86", null, { status: "loading" }),
    externalResult(4, "100.124.44.54"),
  ]) {
    const html = renderer.renderVpn(entry(), { external: { 4: result } });
    assert.doesNotMatch(familyHTML(html, 4), /good|Совпадает с VPS|Другой адрес/);
    assert.doesNotMatch(deviceHTML(html), /Совпадает с VPS|Другой адрес/);
  }
  assert.match(deviceHTML(renderer.renderVpn(entry(), {
    external: { 4: externalResult(4, "89.22.228.86", null, { status: "stale" }) },
  })), /IPv4 миниаппа: Нужна новая проверка/);
  assert.doesNotMatch(familyHTML(renderer.renderVpn(entry(), { external, needsCheck: true }), 4), /good|Совпадает с VPS/);

  const publicRoute = snapshot();
  publicRoute.connection = { state: "via_vps", observed_ip: "89.22.228.86", family: 4 };
  assert.match(renderer.renderVpn(entry(publicRoute)), /Этот запрос идёт через VPS/);
  const ipv6 = snapshot();
  ipv6.connection = { state: "via_tailnet", observed_ip: "fd7a:115c:a1e0::a1", family: 6 };
  assert.match(renderer.renderVpn(entry(ipv6)), /Подключение к VPS по Tailscale/);
  const unknownRoute = snapshot();
  unknownRoute.connection = { state: "unknown", observed_ip: null, family: null };
  assert.match(renderer.renderVpn(entry(unknownRoute)), /Маршрут не подтверждён/);
  const differentNode = snapshot();
  differentNode.server = { state: "different_node", name: "different-host", matches_expected: false, online: null, exit_node_available: null };
  const differentNodeHTML = renderer.renderVpn(entry(differentNode));
  assert.match(differentNodeHTML, /Проверяется другой узел/);
  assert.match(differentNodeHTML, /different-host/);
  assert.match(differentNodeHTML, /Состояние выбранного VPS неизвестно/);
  for (const options of [{ loading: true }, { error: { status: 503 } }]) {
    const html = renderer.renderVpn(entry(snapshot(), options));
    assert.match(html, /IP последней проверки/);
    assert.match(html, /Текущее состояние не подтверждено/);
  }
  assert.match(renderer.renderVpn(entry({ ...snapshot(), enabled: false })), /Панель VPN ещё не настроена/);
  for (const status of [401, 403]) {
    assert.doesNotMatch(renderer.renderVpn(entry(snapshot(), { error: { status } }), { external }), /89\.22\.228\.86|economic-peach|100\.124\.44\.54|data-vpn-check-external/);
  }
  const malicious = snapshot();
  malicious.exit_node.name = '<img src=x onerror="alert(1)">';
  malicious.exit_node.expected_ipv4 = '<script>alert(2)</script>';
  malicious.devices[0].name = '<svg onload="alert(3)">';
  malicious.connection.observed_ip = '<a href="javascript:alert(4)">';
  malicious.connection.checked_at = "invalid timestamp";
  const escaped = renderer.renderVpn(entry(malicious), { external: { 4: externalResult(4, '<img src=x onerror="alert(5)">') } });
  assert.doesNotMatch(escaped, /<img|<script|<svg|href="javascript:/);
  assert.match(escaped, /&lt;img/);

  const ids = [
    "view", "loading-screen", "error-screen", "error-message", "page-title", "date-label",
    "refresh-button", "retry-button", "add-task-button", "help-button", "task-dialog",
    "task-form", "setup-dialog", "setup-content", "toast", "offline-banner",
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new FakeElement()]));
  const navItems = ["today", "planner", "tasks", "health", "profile"].map((view) => new FakeElement({ view }));
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
      initData: "signed-telegram-init-data", ready() {}, expand() {},
      BackButton: {
        show() { backVisible = true; }, hide() { backVisible = false; },
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
  global.setInterval = (callback, milliseconds) => { timers.push({ callback, milliseconds }); return { unref() {} }; };
  global.fetch = (path, options) => {
    if (path.startsWith("api/v1/state")) return Promise.resolve(response({ now: "2026-11-03T12:00:00Z", user: {}, timeline: [] }));
    const request = { path, options, ...deferred() };
    requests.push(request);
    return request.promise;
  };
  const vpnRequests = () => requests.filter((request) => request.path === "api/v1/vpn");
  const externalRequests = () => requests.filter((request) => request.path.startsWith("https://"));
  const latestVpn = () => vpnRequests().at(-1);
  const click = (dataset = {}, attributes = []) => listeners.click({ target: {
    closest: () => ({ dataset, hasAttribute: (name) => attributes.includes(name) }),
  } });
  const checkExternal = async () => { click({}, ["data-vpn-check-external"]); await flush(); return externalRequests().slice(-2); };
  const authorize = async (data = snapshot()) => { latestVpn().resolve(response(data)); await flush(); };
  const refresh = async () => { elements["refresh-button"].listeners.click(); await flush(); };
  const privateHidden = () => assert.doesNotMatch(elements.view.innerHTML, /economic-peach|89\.22\.228\.86|100\.124\.44\.54|data-vpn-check-external/);

  await import("../bot/miniapp/app.mjs?v=vpn-tests");
  await flush();
  navItems.find((item) => item.dataset.view === "profile").listeners.click();
  await flush();
  requests.at(-1).resolve(response(profile()));
  await flush();
  click({ navView: "vpn" });
  await flush();
  assert.equal(elements["page-title"].textContent, "Мой VPN");
  assert.equal(vpnRequests().length, 1);
  assert.equal(latestVpn().options.headers.Authorization, "tma signed-telegram-init-data");
  assert.equal(backVisible, true);
  assert.equal(navItems.find((item) => item.dataset.view === "profile").classNames.has("active"), true);
  assert.equal(navItems.length, 5);
  await checkExternal();
  assert.equal(externalRequests().length, 0, "an in-flight owner check cannot authorize third-party probes");
  await authorize();
  assert.match(elements.view.innerHTML, /Подключение к VPS по Tailscale/);
  assert.equal(externalRequests().length, 0, "opening the panel never contacts third-party IP services");
  await refresh();
  assert.equal(vpnRequests().length, 2);
  assert.equal(externalRequests().length, 0);
  latestVpn().resolve(response({ error: "probe unavailable" }, 503));
  await flush();
  assert.match(elements.view.innerHTML, /Не удалось обновить проверку/);
  await checkExternal();
  assert.equal(externalRequests().length, 0, "stale backend data cannot authorize probes");
  click({}, ["data-retry-current"]);
  await flush();
  await authorize();
  assert.equal(externalRequests().length, 0, "connection refresh stays server-only");

  const first = await checkExternal();
  assert.equal(first.length, 2);
  assert.equal(vpnRequests().length, 3, "manual external check does not use the private API");
  assert.deepEqual(first.map((request) => new URL(request.path).origin).sort(), ["https://api.ipify.org", "https://api6.ipify.org"]);
  for (const request of first) {
    assert.equal(request.options.credentials, "omit");
    assert.doesNotMatch(JSON.stringify(request.options), /signed-telegram-init-data|Authorization|initData/);
  }
  first[0].resolve(response({ ip: "89.22.228.86" }));
  await flush();
  assert.match(familyHTML(elements.view.innerHTML, 4), /Совпадает с VPS/);
  assert.match(familyHTML(elements.view.innerHTML, 6), /Проверяем…/);
  first[1].resolve(response({}, 503));
  await flush();
  assert.match(familyHTML(elements.view.innerHTML, 4), /Совпадает с VPS/);
  assert.match(familyHTML(elements.view.innerHTML, 6), /Проверка недоступна/);
  const originalNow = Date.now;
  try {
    Date.now = () => originalNow() + 46_000;
    timers.find((timer) => timer.milliseconds === 5_000).callback();
    assert.match(familyHTML(elements.view.innerHTML, 4), /Нужна новая проверка/);
    assert.doesNotMatch(familyHTML(elements.view.innerHTML, 4), /Совпадает с VPS/);
    assert.match(elements.view.innerHTML, /Результат мог измениться/);
    await checkExternal();
    assert.equal(externalRequests().length, 2);
  } finally { Date.now = originalNow; }
  await refresh();
  await authorize();

  for (const lifecycle of ["hide", "offline", "leave", "popstate"]) {
    const pending = await checkExternal();
    const count = externalRequests().length;
    if (lifecycle === "hide") {
      document.visibilityState = "hidden";
      listeners.visibilitychange();
    } else if (lifecycle === "offline") {
      navigator.onLine = false;
      listeners["window:offline"]();
    } else if (lifecycle === "leave") {
      elements["help-button"].listeners.click();
      assert.equal(elements["page-title"].textContent, "Справка");
    } else {
      window.location.hash = "#profile";
      listeners["window:popstate"]();
    }
    for (const request of pending) {
      assert.equal(request.options.signal.aborted, true, `${lifecycle} cancels external probes`);
      request.resolve(response({ ip: request.path.includes("api6.") ? "2a0b:4140:5020::2" : "89.22.228.86" }));
    }
    await flush();
    assert.doesNotMatch(elements.view.innerHTML, /Совпадает с VPS/);
    if (lifecycle === "hide") {
      document.visibilityState = "visible";
      listeners.visibilitychange();
    } else if (lifecycle === "offline") {
      navigator.onLine = true;
      listeners["window:online"]();
    } else if (lifecycle === "leave") telegramBack();
    else click({ navView: "vpn" });
    await flush();
    await authorize();
    assert.equal(externalRequests().length, count, "resume/navigation/online never restarts external probes");
    assert.doesNotMatch(elements.view.innerHTML, /Совпадает с VPS/);
  }

  const configProbe = await checkExternal();
  configProbe[0].resolve(response({ ip: "89.22.228.86" }));
  configProbe[1].resolve(response({ ip: "2a0b:4140:5020::2" }));
  await flush();
  assert.match(familyHTML(elements.view.innerHTML, 4), /Совпадает с VPS/);
  await refresh();
  const changed = snapshot();
  changed.exit_node.expected_ipv4 = "1.1.1.1";
  await authorize(changed);
  assert.match(familyHTML(elements.view.innerHTML, 4), /Нужна новая проверка/);
  assert.doesNotMatch(familyHTML(elements.view.innerHTML, 4), /Другой адрес|Совпадает с VPS/);
  const changedProbe = await checkExternal();
  changedProbe[0].resolve(response({ ip: "89.22.228.86" }));
  changedProbe[1].resolve(response({ ip: "2a0b:4140:5020::2" }));
  await flush();
  assert.match(familyHTML(elements.view.innerHTML, 4), /Другой адрес/);
  assert.match(familyHTML(elements.view.innerHTML, 6), /Совпадает с VPS/);

  for (const status of [403, 401]) {
    const pending = await checkExternal();
    await refresh();
    latestVpn().resolve(response({ error: "owner authorization lost" }, status));
    await flush();
    privateHidden();
    const count = externalRequests().length;
    await checkExternal();
    assert.equal(externalRequests().length, count);
    for (const request of pending) {
      assert.equal(request.options.signal.aborted, true);
      request.resolve(response({ ip: "89.22.228.86" }));
    }
    await flush();
    privateHidden();
    await refresh();
    privateHidden();
    latestVpn().resolve(response({ error: "still unavailable" }, 503));
    await flush();
    privateHidden();
    await refresh();
    privateHidden();
    await authorize();
    assert.match(elements.view.innerHTML, /Подключение к VPS по Tailscale/);
    assert.match(familyHTML(elements.view.innerHTML, 4), /Не проверен/);
  }
  const beforeAuto = externalRequests().length;
  timers.find((timer) => timer.milliseconds === 90_000).callback();
  await flush();
  await authorize();
  assert.equal(externalRequests().length, beforeAuto, "periodic refresh never contacts third parties");
  telegramBack();
  assert.equal(elements["page-title"].textContent, "Профиль");
  assert.equal(backVisible, false);
  console.log("Mini App VPN tailnet, explicit per-family external checks, authorization and lifecycle checks passed.");
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
