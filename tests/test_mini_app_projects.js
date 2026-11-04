"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

function projects(url = "https://panel.example.test/813spam/", available = true) {
  return { projects: [
    { id: "vpn", name: "VPN", description: "Сервер, устройства и внешний IP", kind: "internal", view: "vpn", available: true },
    { id: "813spam", name: "813spam", description: "Панель проекта", kind: "external", url, available, automatic_login_available: false },
  ] };
}

function entry(data = projects(), extra = {}) {
  return { data, loading: false, error: null, ...extra };
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
}

async function flush() { await new Promise((resolve) => setImmediate(resolve)); }

async function run() {
  const { createViewRenderer } = await import("../bot/miniapp/views.mjs?v=7");
  const renderer = createViewRenderer({ getTimezone: () => "Europe/Moscow", periods: {} });
  const html = renderer.renderProjects(entry());
  assert.match(html, /data-project-id="vpn"[\s\S]*?data-nav-view="vpn"/);
  assert.match(html, /data-project-id="813spam"/);
  assert.match(html, /<a[^>]*href="https:\/\/panel\.example\.test\/813spam\/"[^>]*target="_blank"[^>]*rel="noopener noreferrer"/);
  assert.match(html, /Автовход пока не подключён/);
  assert.doesNotMatch(html, /password|initData|Authorization|data-copy-input|<input/);
  const unexpectedAutoLogin = projects();
  unexpectedAutoLogin.projects[1].automatic_login_available = true;
  assert.match(renderer.renderProjects(entry(unexpectedAutoLogin)), /Автовход пока не подключён/);

  const unsafeUrls = [
    null, "", "http://panel.example.test/", "javascript:alert(1)", "//panel.example.test/", "/panel",
    "https://user:secret@panel.example.test/", "https://user@panel.example.test/", "https://@panel.example.test/",
    "https://panel.example.test/?password=secret", "https://panel.example.test/?", "https://panel.example.test/#secret", "https://panel.example.test/#",
    " https://panel.example.test/", "https://panel.example.test/\n", "https://panel.\texample.test/", "https://panel.example.test/\u0000",
    "https://panel.example.test\\@evil.example.test/", "data:text/html,test", "https:///",
  ];
  for (const url of unsafeUrls) {
    const unsafe = renderer.renderProjects(entry(projects(url)));
    assert.doesNotMatch(unsafe, /<a\b/, `reject unsafe project URL ${JSON.stringify(url)}`);
    assert.match(unsafe, /disabled>Ссылка не настроена/);
    assert.doesNotMatch(unsafe, /secret|evil\.example/);
  }
  assert.doesNotMatch(renderer.renderProjects(entry(projects(undefined, false))), /<a\b/);
  const escaped = projects('https://panel.example.test/<"test">');
  escaped.projects[0].description = '<img src=x onerror="alert(1)">';
  escaped.projects[1].description = '<script>alert(1)</script>';
  const escapedHtml = renderer.renderProjects(entry(escaped));
  assert.doesNotMatch(escapedHtml, /<img|<script/);
  assert.match(escapedHtml, /&lt;img/);
  assert.match(escapedHtml, /href="https:\/\/panel\.example\.test\/%3C%22test%22%3E"/);
  assert.match(renderer.renderProjects(entry(null, { loading: true })), /Собираю актуальные данные/);
  assert.match(renderer.renderProjects(entry({ projects: [null, false, 42, "unknown", { id: "other" }] })), /Проекты пока не добавлены/);
  const withUnknown = projects();
  withUnknown.projects.splice(1, 0, null);
  assert.match(renderer.renderProjects(entry(withUnknown)), /Открыть 813spam/);
  assert.match(renderer.renderProjects(entry(null, { error: { status: 503, message: "Временно недоступно" } })), /Повторить/);
  assert.match(renderer.renderProjects(entry(projects(), { error: { status: 503 } })), /Показаны последние данные/);
  for (const status of [401, 403]) {
    const denied = renderer.renderProjects(entry(projects(), { error: { status } }));
    assert.doesNotMatch(denied, /panel\.example|data-nav-view="vpn"/);
    assert.match(denied, status === 401 ? /Открой Mini App/ : /Проекты доступны владельцу/);
  }

  const profile = { name: "Owner", timezone_options: [], planning: {}, hse: {} };
  for (const flags of [{ projects_available: true }, { vpn_panel_available: true }]) {
    assert.match(renderer.renderProfile({ ...profile, ...flags }), /data-nav-view="projects"/);
  }
  assert.doesNotMatch(renderer.renderProfile(profile), /data-nav-view="projects"|data-nav-view="vpn"/);
  const index = fs.readFileSync(path.join(__dirname, "../bot/miniapp/index.html"), "utf8");
  assert.equal((index.match(/class="nav-item(?: active)?"/g) || []).length, 6);
  assert.match(index, /data-view="projects"/);

  const ids = [
    "view", "loading-screen", "error-screen", "error-message", "page-title", "date-label",
    "refresh-button", "retry-button", "add-task-button", "help-button", "task-dialog",
    "task-form", "setup-dialog", "setup-content", "toast", "offline-banner",
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, new FakeElement()]));
  const navItems = ["today", "planner", "tasks", "health", "projects", "profile"].map((view) => new FakeElement({ view }));
  const requests = [];
  const listeners = {};
  const storedViews = [];
  global.window = {
    location: { pathname: "/assistant/", search: "?debug_user=42", hash: "" },
    history: { pushState() {}, replaceState() {} },
    sessionStorage: { getItem() { return null; }, setItem(_key, value) { storedViews.push(value); } },
    addEventListener(name, callback) { listeners[`window:${name}`] = callback; },
    scrollTo() {},
    Telegram: { WebApp: { initData: "private-telegram-auth", ready() {}, expand() {} } },
  };
  Object.defineProperty(global, "navigator", { configurable: true, value: { onLine: true } });
  global.document = {
    visibilityState: "visible",
    addEventListener(name, callback) { listeners[name] = callback; },
    querySelector(selector) { return selector.startsWith("#") ? elements[selector.slice(1)] || null : null; },
    querySelectorAll(selector) { return selector === ".nav-item" ? navItems : []; },
  };
  global.setInterval = () => ({ unref() {} });
  global.fetch = (requestPath, options) => {
    const request = { path: requestPath, options, ...deferred() };
    requests.push(request);
    return request.promise;
  };
  const latest = (requestPath) => requests.filter((request) => request.path === requestPath).at(-1);
  const navigate = async (view) => { navItems.find((item) => item.dataset.view === view).listeners.click(); await flush(); };
  const refresh = async () => { elements["refresh-button"].listeners.click(); await flush(); };
  const noPrivateLinks = () => assert.doesNotMatch(elements.view.innerHTML, /panel\.example|data-nav-view="vpn"/);
  const resolveProjects = async (data = projects(), status = 200) => { latest("api/v1/projects").resolve(response(data, status)); await flush(); };

  await import("../bot/miniapp/app.mjs?v=projects-tests");
  await flush();
  assert.equal(requests[0].path, "api/v1/state?period=today");
  await navigate("profile");
  await navigate("projects");
  assert.equal(elements["page-title"].textContent, "Мои проекты");
  assert.equal(navItems.find((item) => item.dataset.view === "projects").classNames.has("active"), true);
  assert.equal(elements["add-task-button"].classNames.has("hidden"), true);
  assert.equal(latest("api/v1/projects").options.headers.Authorization, "tma private-telegram-auth");
  assert.match(elements.view.innerHTML, /Собираю актуальные данные/);
  await resolveProjects();
  assert.match(elements.view.innerHTML, /Открыть 813spam/);
  latest("api/v1/profile").resolve(response(profile));
  latest("api/v1/state?period=today").resolve(response({ now: "2026-11-04T12:00:00Z", user: {}, timeline: [] }));
  await flush();
  assert.equal(elements["page-title"].textContent, "Мои проекты");
  assert.match(elements.view.innerHTML, /Открыть 813spam/);
  assert.doesNotMatch(elements.view.innerHTML, /private-telegram-auth|debug_user|password|initData/);
  const beforeRefresh = requests.length;
  await refresh();
  assert.equal(requests.length, beforeRefresh + 1);
  assert.equal(requests.at(-1).path, "api/v1/projects");
  await resolveProjects();

  for (const status of [403, 401]) {
    await refresh();
    await resolveProjects({ error: "owner access denied" }, status);
    noPrivateLinks();
    await refresh();
    noPrivateLinks();
    await resolveProjects({ error: "temporarily unavailable" }, 503);
    noPrivateLinks();
    await navigate("today");
    await navigate("projects");
    noPrivateLinks();
    await refresh();
    noPrivateLinks();
    await resolveProjects();
    assert.match(elements.view.innerHTML, /Открыть 813spam/);
  }
  await refresh();
  const older = latest("api/v1/projects");
  await refresh();
  await resolveProjects({ error: "access revoked" }, 403);
  older.resolve(response(projects()));
  await flush();
  noPrivateLinks();
  await refresh();
  await resolveProjects();
  assert.equal(requests.some((request) => /^https?:/.test(request.path)), false, "catalog never fetches the external project or relays Telegram credentials");
  assert.equal(storedViews.at(-1), "projects");
  console.log("Mini App Projects navigation, owner authorization, safe external links and resource isolation checks passed.");
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
