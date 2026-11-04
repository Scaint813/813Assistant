import { ApiClient, initializeTelegram } from "./api_client.mjs";
import { ResourceStore } from "./resource_store.mjs";
import { createViewRenderer } from "./views.mjs?v=6";
import { probeExternalIp } from "./external_ip.mjs";

const telegram = window.Telegram?.WebApp;
const VIEW_NAMES = new Set(["today", "planner", "tasks", "health", "profile", "help", "vpn"]);
const TITLES = {
  today: "Сегодня",
  planner: "План",
  tasks: "Задачи",
  health: "Здоровье",
  profile: "Профиль",
  help: "Справка",
  vpn: "Мой VPN",
};
const PERIODS = { today: "Сегодня", tomorrow: "Завтра", week: "Неделя" };
const AUTO_REFRESH_MS = 90_000;
const STALE_AFTER_MS = 45_000;

function storedView() {
  const hash = (window.location?.hash || "").replace(/^#/, "");
  if (VIEW_NAMES.has(hash)) return hash;
  try {
    const saved = window.sessionStorage?.getItem("813assistant:view");
    return VIEW_NAMES.has(saved) && saved !== "help" ? saved : "today";
  } catch (_error) {
    return "today";
  }
}

const state = {
  view: storedView(),
  previousView: "today",
  period: "today",
  shortcutConfig: null,
  healthShortcutConfig: null,
  timezone: undefined,
  vpnNeedsCheck: true,
  vpnAccessDeniedStatus: null,
  vpnExternal: { 4: { status: "idle" }, 6: { status: "idle" } },
};
let externalGeneration = 0;
let externalController = null;
let externalExpectedSignature = null;

function invalidateExternalIp({ clear = false } = {}) {
  externalGeneration += 1;
  externalController?.abort();
  externalController = null;
  for (const family of [4, 6]) {
    const previous = state.vpnExternal[family];
    state.vpnExternal[family] = clear || previous.status === "idle"
      ? { status: "idle" }
      : { ...previous, status: "stale" };
  }
}

function expectedIpSignature(data) {
  return JSON.stringify([data?.exit_node?.expected_ipv4 || null, data?.exit_node?.expected_ipv6 || null]);
}

const els = {
  view: document.querySelector("#view"),
  loading: document.querySelector("#loading-screen"),
  error: document.querySelector("#error-screen"),
  errorMessage: document.querySelector("#error-message"),
  title: document.querySelector("#page-title"),
  date: document.querySelector("#date-label"),
  refresh: document.querySelector("#refresh-button"),
  help: document.querySelector("#help-button"),
  retry: document.querySelector("#retry-button"),
  add: document.querySelector("#add-task-button"),
  taskDialog: document.querySelector("#task-dialog"),
  taskForm: document.querySelector("#task-form"),
  setupDialog: document.querySelector("#setup-dialog"),
  setupContent: document.querySelector("#setup-content"),
  toast: document.querySelector("#toast"),
  offline: document.querySelector("#offline-banner"),
};

const api = new ApiClient({ telegram, location: window.location });
const renderer = createViewRenderer({
  getTimezone: () => state.timezone,
  periods: PERIODS,
});

function scheduleKey(period) {
  return `schedule:${period}`;
}

function currentResourceKey() {
  if (state.view === "help") return null;
  if (state.view === "profile") return "profile";
  if (state.view === "health") return "health";
  if (state.view === "vpn") return "vpn";
  return scheduleKey(state.view === "planner" ? state.period : "today");
}

function loadResource(key) {
  if (key === "profile") return api.request("api/v1/profile");
  if (key === "health") return api.request("api/v1/health");
  if (key === "vpn") return api.request("api/v1/vpn");
  return api.request(`api/v1/state?period=${key.slice("schedule:".length)}`);
}

const resources = new ResourceStore(loadResource, (key, entry) => {
  if (key === "vpn") {
    if ([401, 403].includes(entry.error?.status)) {
      state.vpnAccessDeniedStatus = entry.error.status;
      invalidateExternalIp({ clear: true });
    } else if (entry.loading) {
      invalidateExternalIp();
    } else if (entry.data !== null && !entry.loading && !entry.error) {
      const signature = expectedIpSignature(entry.data);
      if (signature !== externalExpectedSignature) invalidateExternalIp();
      externalExpectedSignature = signature;
      state.vpnAccessDeniedStatus = null;
      if (document.visibilityState !== "hidden" && navigator.onLine !== false) state.vpnNeedsCheck = false;
    }
  }
  if (key === currentResourceKey()) render();
});

function setTelegramBackButton() {
  const shouldShow = ["help", "vpn"].includes(state.view);
  if (shouldShow) telegram?.BackButton?.show?.();
  else telegram?.BackButton?.hide?.();
}

function setHeader() {
  els.title.textContent = TITLES[state.view];
  document.querySelectorAll(".nav-item").forEach((button) => {
    const active = button.dataset.view === (state.view === "vpn" ? "profile" : state.view);
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  });
  els.help?.classList.toggle("hidden", state.view === "help");
  els.refresh.classList.toggle("hidden", state.view === "help");
  els.add.classList.toggle("hidden", ["health", "profile", "help", "tasks", "vpn"].includes(state.view));
  setTelegramBackButton();
}

function render() {
  setHeader();
  els.loading.classList.add("hidden");
  els.error.classList.add("hidden");
  els.view.classList.remove("hidden");

  if (state.view === "help") {
    els.date.textContent = "FAQ И ПОДКЛЮЧЕНИЯ";
    els.view.innerHTML = renderer.renderHelp();
    return;
  }

  const key = currentResourceKey();
  const entry = resources.get(key);
  els.refresh.classList.toggle("spinning", entry.loading);

  if (state.view === "vpn") {
    els.date.textContent = "ПОДКЛЮЧЕНИЕ ЭТОГО УСТРОЙСТВА";
    els.view.innerHTML = renderer.renderVpn(entry, {
      needsCheck: state.vpnNeedsCheck || navigator.onLine === false
        || (entry.data !== null && Date.now() - entry.updatedAt > STALE_AFTER_MS),
      accessDeniedStatus: state.vpnAccessDeniedStatus,
      external: state.vpnExternal,
      externalCheckEnabled: canCheckExternalIp(),
    });
    return;
  }

  if (entry.data === null) {
    if (state.view === "planner") {
      els.view.innerHTML = renderer.periodControlHTML(state.period) + renderer.resourceStatusHTML(entry);
    } else {
      els.view.innerHTML = renderer.resourceStatusHTML(entry);
    }
    return;
  }

  if (state.view === "profile") {
    state.timezone = entry.data.timezone;
    els.date.textContent = "НАСТРОЙКИ И АВТОМАТИЗАЦИЯ";
    els.view.innerHTML = renderer.renderProfile(entry.data);
    return;
  }
  if (state.view === "health") {
    state.timezone = entry.data.timezone;
    els.date.textContent = "ВОССТАНОВЛЕНИЕ И ТРЕНИРОВКИ";
    els.view.innerHTML = renderer.renderHealth(entry.data);
    return;
  }

  state.timezone = entry.data.user.timezone;
  els.date.textContent = new Intl.DateTimeFormat("ru-RU", {
    weekday: "long",
    day: "numeric",
    month: "long",
    timeZone: state.timezone,
  }).format(new Date(entry.data.now));
  if (state.view === "planner") {
    els.view.innerHTML = renderer.renderPlanner(entry.data, state.period);
  } else if (state.view === "tasks") {
    els.view.innerHTML = renderer.renderTasks(entry.data);
  } else {
    els.view.innerHTML = renderer.renderToday(entry.data);
  }
}

function toast(message) {
  els.toast.textContent = message;
  els.toast.classList.add("visible");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => els.toast.classList.remove("visible"), 2_600);
}

function loadSchedule(period, force = false) {
  return resources.load(scheduleKey(period), force);
}

function loadCurrent(force = false) {
  const key = currentResourceKey();
  if (!key) return Promise.resolve(null);
  return resources.load(key, force);
}

function refreshCurrent(force = false) {
  if (navigator.onLine === false) return;
  loadCurrent(force).catch(() => {});
}

function canCheckExternalIp() {
  const entry = resources.get("vpn");
  return state.view === "vpn" && document.visibilityState !== "hidden"
    && navigator.onLine !== false && !state.vpnAccessDeniedStatus
    && !state.vpnNeedsCheck && entry.data?.enabled === true
    && !entry.loading && !entry.error && Date.now() - entry.updatedAt <= STALE_AFTER_MS;
}

async function checkExternalIp() {
  if (!canCheckExternalIp()) return;
  invalidateExternalIp({ clear: true });
  const generation = externalGeneration;
  const signature = expectedIpSignature(resources.get("vpn").data);
  const controller = new AbortController();
  externalController = controller;
  state.vpnExternal = { 4: { status: "loading" }, 6: { status: "loading" } };
  render();
  await Promise.all([4, 6].map(async (family) => {
    let result;
    try {
      result = await probeExternalIp(family, { signal: controller.signal });
    } catch (_error) {
      result = { family, observed_ip: null, checked_at: null, provider: null, error: "unavailable" };
    }
    if (generation !== externalGeneration || controller.signal.aborted || !canCheckExternalIp()
        || signature !== expectedIpSignature(resources.get("vpn").data)) return;
    state.vpnExternal[family] = { ...result, status: "complete", updatedAt: Date.now() };
    render();
  }));
  if (generation === externalGeneration) externalController = null;
}

function ensureCurrent() {
  const key = currentResourceKey();
  if (!key) return;
  const entry = resources.get(key);
  if (key === "vpn") {
    refreshCurrent(true);
    return;
  }
  const stale = entry.data !== null && Date.now() - entry.updatedAt > STALE_AFTER_MS;
  refreshCurrent(stale);
}

function navigate(view, { replace = false } = {}) {
  if (!VIEW_NAMES.has(view)) return;
  if (state.view === "vpn" && view !== "vpn") invalidateExternalIp();
  if (view !== "help") state.previousView = state.view === "help" ? state.previousView : state.view;
  else if (state.view !== "help") state.previousView = state.view;
  state.view = view;
  if (view === "vpn") state.vpnNeedsCheck = true;
  try {
    if (view !== "help") window.sessionStorage?.setItem("813assistant:view", view);
    const url = `${window.location.pathname || ""}${window.location.search || ""}#${view}`;
    if (replace) window.history?.replaceState?.({ view }, "", url);
    else window.history?.pushState?.({ view }, "", url);
  } catch (_error) {
    // Navigation still works if a restrictive WebView disables storage/history.
  }
  render();
  ensureCurrent();
  window.scrollTo?.({ top: 0, behavior: "smooth" });
}

function goBack() {
  if (state.view === "vpn") navigate("profile", { replace: true });
  else if (state.view === "help") navigate(state.previousView || "profile", { replace: true });
}

function openTaskDialog() {
  els.taskForm.reset();
  document.querySelector("#task-duration").value = 30;
  els.taskDialog.showModal();
  setTimeout(() => document.querySelector("#task-title")?.focus(), 80);
}

async function mutateTask(button) {
  const row = button.closest("[data-task-id]");
  if (!row) return;
  const complete = button.dataset.taskAction === "complete";
  button.disabled = true;
  try {
    await api.request(`api/v1/tasks/${row.dataset.taskId}`, {
      method: "PATCH",
      body: JSON.stringify({ status: complete ? "completed" : "active" }),
    });
    resources.invalidatePrefix("schedule:");
    await loadSchedule("today", true);
    telegram?.HapticFeedback?.notificationOccurred("success");
    toast(complete ? "Задача завершена. Её можно вернуть сегодня." : "Задача возвращена");
  } catch (error) {
    toast(error.message);
    button.disabled = false;
  }
}

async function showConnection(kind, button) {
  button.disabled = true;
  try {
    if (kind === "hse" && !state.shortcutConfig) {
      state.shortcutConfig = await api.request("api/v1/hse/shortcut-config");
    }
    if (kind === "health" && !state.healthShortcutConfig) {
      state.healthShortcutConfig = await api.request("api/v1/health/shortcut-config");
    }
    const config = kind === "health" ? state.healthShortcutConfig : state.shortcutConfig;
    els.setupContent.innerHTML = renderer.connectionSetupHTML(kind, config);
    els.setupDialog.showModal();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
}

async function copyInput(id) {
  const input = document.getElementById(id);
  if (!input) return;
  try {
    await navigator.clipboard.writeText(input.value);
  } catch (_error) {
    const originalType = input.type;
    input.type = "text";
    input.select();
    document.execCommand("copy");
    input.type = originalType;
  }
  telegram?.HapticFeedback?.selectionChanged();
  toast("Скопировано");
}

async function updateIntake(button) {
  button.disabled = true;
  try {
    const payload = { [button.dataset.intakeField]: Number(button.dataset.intakeDelta) };
    const health = await api.request("api/v1/health/intake", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    resources.set("health", health);
    telegram?.HapticFeedback?.selectionChanged();
  } catch (error) {
    toast(error.message);
    button.disabled = false;
  }
}

async function saveProfile(form) {
  const button = form.querySelector("button[type=submit]");
  const data = new FormData(form);
  button.disabled = true;
  try {
    const profile = await api.request("api/v1/profile", {
      method: "PATCH",
      body: JSON.stringify({
        timezone: data.get("timezone"),
        home: data.get("home") || "",
        daily_focus_minutes: Number(data.get("daily_focus_minutes") || 180),
      }),
    });
    resources.set("profile", profile);
    resources.invalidatePrefix("schedule:");
    toast("Профиль сохранён, автоплан обновлён");
    telegram?.HapticFeedback?.notificationOccurred("success");
  } catch (error) {
    toast(error.message);
    button.disabled = false;
  }
}

async function importHSE(form) {
  const button = form.querySelector("button[type=submit]");
  const file = new FormData(form).get("calendar");
  if (!(file instanceof File) || !file.size) return;
  button.disabled = true;
  button.textContent = "Загружаю…";
  try {
    const result = await api.request("api/v1/hse/import", {
      method: "POST",
      body: file,
      headers: { "Content-Type": "text/calendar" },
    });
    resources.invalidate("profile");
    resources.invalidatePrefix("schedule:");
    await resources.load("profile", true);
    toast(`Загружено событий: ${result.imported}`);
    telegram?.HapticFeedback?.notificationOccurred("success");
  } catch (error) {
    toast(error.message);
    button.disabled = false;
    button.textContent = "Загрузить .ics";
  }
}

async function saveHealthGoals(form) {
  const button = form.querySelector("button[type=submit]");
  const data = new FormData(form);
  button.disabled = true;
  try {
    const health = await api.request("api/v1/health/goals", {
      method: "PATCH",
      body: JSON.stringify({
        water_ml: Number(data.get("water_ml")),
        protein_g: Number(data.get("protein_g")),
      }),
    });
    resources.set("health", health);
    toast("Цели сохранены");
  } catch (error) {
    toast(error.message);
    button.disabled = false;
  }
}

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => navigate(button.dataset.view));
});

document.addEventListener("click", (event) => {
  const target = event.target.closest?.("button, [data-nav-view]");
  if (!target) return;
  if (target.dataset.navView) navigate(target.dataset.navView);
  else if (target.hasAttribute("data-open-task")) openTaskDialog();
  else if (target.hasAttribute("data-open-help")) {
    els.setupDialog?.close();
    navigate("help");
  } else if (target.hasAttribute("data-back")) goBack();
  else if (target.dataset.period) {
    state.period = target.dataset.period;
    render();
    refreshCurrent(false);
  } else if (target.hasAttribute("data-vpn-check-external")) checkExternalIp();
  else if (target.hasAttribute("data-retry-current")) refreshCurrent(true);
  else if (target.dataset.taskAction) mutateTask(target);
  else if (target.dataset.connection) showConnection(target.dataset.connection, target);
  else if (target.dataset.copyInput) copyInput(target.dataset.copyInput);
  else if (target.dataset.intakeField) updateIntake(target);
  else if (target.hasAttribute("data-close-setup")) els.setupDialog.close();
});

document.addEventListener("submit", (event) => {
  const form = event.target;
  if (form.id === "task-form") return;
  event.preventDefault();
  if (form.id === "profile-form") saveProfile(form);
  else if (form.id === "hse-form") importHSE(form);
  else if (form.id === "health-goals-form") saveHealthGoals(form);
});

els.taskForm.addEventListener("submit", async (event) => {
  if (event.submitter?.value === "cancel") return;
  event.preventDefault();
  if (!els.taskForm.reportValidity()) return;
  const button = document.querySelector("#save-task-button");
  const data = new FormData(els.taskForm);
  button.disabled = true;
  try {
    await api.request("api/v1/tasks", {
      method: "POST",
      body: JSON.stringify({
        title: data.get("title"),
        deadline: data.get("deadline") || null,
        estimated_minutes: Number(data.get("estimated_minutes") || 30),
      }),
    });
    resources.invalidatePrefix("schedule:");
    await loadSchedule(state.view === "planner" ? state.period : "today", true);
    els.taskDialog.close();
    toast("Задача добавлена в автоплан");
    telegram?.HapticFeedback?.notificationOccurred("success");
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

[els.taskDialog, els.setupDialog].forEach((dialog) => {
  dialog?.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
});

els.refresh.addEventListener("click", () => refreshCurrent(true));
els.help?.addEventListener("click", () => navigate(state.view === "help" ? state.previousView : "help"));
els.retry.addEventListener("click", () => refreshCurrent(true));
els.add.addEventListener("click", openTaskDialog);
telegram?.BackButton?.onClick?.(goBack);

window.addEventListener?.("popstate", () => {
  if (state.view === "vpn") invalidateExternalIp();
  const view = (window.location.hash || "").replace(/^#/, "");
  state.view = VIEW_NAMES.has(view) ? view : "today";
  if (state.view === "vpn") state.vpnNeedsCheck = true;
  render();
  ensureCurrent();
});
document.addEventListener("visibilitychange", () => {
  if (state.view === "vpn") {
    invalidateExternalIp();
    state.vpnNeedsCheck = true;
    render();
  }
  if (document.visibilityState === "visible") refreshCurrent(true);
});
window.addEventListener?.("online", () => {
  els.offline?.classList.add("hidden");
  refreshCurrent(true);
});
window.addEventListener?.("offline", () => {
  els.offline?.classList.remove("hidden");
  if (state.view === "vpn") {
    invalidateExternalIp();
    state.vpnNeedsCheck = true;
    render();
  }
});

initializeTelegram(telegram);
els.date.textContent = new Intl.DateTimeFormat("ru-RU", {
  weekday: "long",
  day: "numeric",
  month: "long",
}).format(new Date());
navigate(state.view, { replace: true });
const refreshTimer = setInterval(() => {
  if (document.visibilityState === "visible") refreshCurrent(true);
}, AUTO_REFRESH_MS);
refreshTimer.unref?.();

const vpnFreshnessTimer = setInterval(() => {
  if (state.view !== "vpn" || document.visibilityState !== "visible") return;
  const entry = resources.get("vpn");
  let changed = false;
  if (!state.vpnNeedsCheck && entry.data !== null && !entry.loading
      && Date.now() - entry.updatedAt > STALE_AFTER_MS) {
    state.vpnNeedsCheck = true;
    invalidateExternalIp();
    changed = true;
  }
  for (const family of [4, 6]) {
    const result = state.vpnExternal[family];
    if (result.status === "complete" && Date.now() - result.updatedAt > STALE_AFTER_MS) {
      state.vpnExternal[family] = { ...result, status: "stale" };
      changed = true;
    }
  }
  if (changed) render();
}, 5_000);
vpnFreshnessTimer.unref?.();
