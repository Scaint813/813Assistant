(() => {
  "use strict";

  const tg = window.Telegram?.WebApp;
  const state = {
    view: "today",
    period: "today",
    shortcutConfig: null,
    timezone: undefined,
  };
  const titles = { today: "Сегодня", planner: "Планнер", tasks: "Задачи", profile: "Профиль" };
  const periods = { today: "Сегодня", tomorrow: "Завтра", week: "Неделя" };
  const els = {
    view: document.querySelector("#view"),
    loading: document.querySelector("#loading-screen"),
    error: document.querySelector("#error-screen"),
    errorMessage: document.querySelector("#error-message"),
    title: document.querySelector("#page-title"),
    date: document.querySelector("#date-label"),
    refresh: document.querySelector("#refresh-button"),
    retry: document.querySelector("#retry-button"),
    add: document.querySelector("#add-task-button"),
    dialog: document.querySelector("#task-dialog"),
    form: document.querySelector("#task-form"),
    toast: document.querySelector("#toast"),
  };
  const resources = new window.MiniAppResourceStore(
    loadResource,
    (key) => {
      if (key === currentResourceKey()) render();
    },
  );

  function initializeTelegram() {
    if (!tg) return;
    tg.ready();
    tg.expand();
    tg.enableClosingConfirmation?.();
    const bg = tg.themeParams?.secondary_bg_color || "#f4f5f7";
    tg.setHeaderColor?.(bg);
    tg.setBackgroundColor?.(bg);
    tg.setBottomBarColor?.(tg.themeParams?.bg_color || "#ffffff");
  }

  function apiHeaders(json = false) {
    const headers = {};
    if (tg?.initData) headers.Authorization = `tma ${tg.initData}`;
    const debugUser = new URLSearchParams(location.search).get("debug_user");
    if (debugUser) headers["X-Debug-User"] = debugUser;
    if (json) headers["Content-Type"] = "application/json";
    return headers;
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(path, {
        ...options,
        signal: controller.signal,
        headers: { ...apiHeaders(Boolean(options.body) && !(options.body instanceof Blob)), ...(options.headers || {}) },
      });
      let payload = {};
      try { payload = await response.json(); } catch (_error) { /* empty response */ }
      if (!response.ok) {
        const error = new Error(payload.error || `Ошибка ${response.status}`);
        error.status = response.status;
        throw error;
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError") {
        const timeoutError = new Error("Раздел не ответил за 10 секунд. Попробуй ещё раз.");
        timeoutError.status = 408;
        throw timeoutError;
      }
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  function scheduleKey(period) { return `schedule:${period}`; }
  function currentResourceKey() {
    if (state.view === "profile") return "profile";
    return scheduleKey(state.view === "planner" ? state.period : "today");
  }
  function loadResource(key) {
    if (key === "profile") return api("api/v1/profile");
    return api(`api/v1/state?period=${key.slice("schedule:".length)}`);
  }

  function escapeHTML(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[character]);
  }

  function parseDate(value) { return value ? new Date(value) : null; }
  function formatTime(value) {
    const date = parseDate(value);
    return date ? new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", timeZone: state.timezone }).format(date) : "";
  }
  function formatDate(value, options = {}) {
    const date = parseDate(value);
    return date ? new Intl.DateTimeFormat("ru-RU", { ...options, timeZone: state.timezone }).format(date) : "";
  }
  function dateKey(value) {
    const date = parseDate(value);
    if (!date) return "";
    const parts = new Intl.DateTimeFormat("en", {
      year: "numeric", month: "2-digit", day: "2-digit", timeZone: state.timezone,
    }).formatToParts(date);
    const partsByType = Object.fromEntries(
      parts.map((part) => [part.type, part.value]),
    );
    return `${partsByType.year}-${partsByType.month}-${partsByType.day}`;
  }
  function plural(number, one, few, many) {
    const n10 = number % 10; const n100 = number % 100;
    if (n10 === 1 && n100 !== 11) return one;
    if (n10 >= 2 && n10 <= 4 && !(n100 >= 12 && n100 <= 14)) return few;
    return many;
  }

  function timelineHTML(items) {
    if (!items.length) return '<div class="empty-state">Здесь свободно. Можно запланировать важную задачу.</div>';
    return `<ol class="timeline">${items.map((item) => {
      const finish = item.end ? `–${formatTime(item.end)}` : "";
      const details = [item.meta, item.location].filter(Boolean).map(escapeHTML).join(" · ");
      return `<li class="timeline-item ${escapeHTML(item.kind)}">
        <time class="time-label" datetime="${escapeHTML(item.start)}">${formatTime(item.start)}${finish}</time>
        <span class="timeline-line" aria-hidden="true"></span>
        <div class="timeline-content"><h3>${escapeHTML(item.title)}</h3>${details ? `<p class="item-meta">${details}</p>` : ""}</div>
      </li>`;
    }).join("")}</ol>`;
  }

  function taskMeta(task) {
    if (task.overdue) return '<span class="task-tag overdue">Срок прошёл</span>';
    if (task.deadline) return `<span class="task-tag">до ${formatDate(task.deadline, { day: "numeric", month: "short" })}, ${formatTime(task.deadline)}</span>`;
    if (task.planning_state === "inbox") return '<span class="task-tag">нужно уточнить</span>';
    return `<span class="task-tag">${task.estimated_minutes || 30} мин</span>`;
  }

  function taskRowsHTML(tasks, completed = false) {
    if (!tasks.length) return '<div class="empty-state">Нет задач в этом разделе.</div>';
    return `<ul class="task-list">${tasks.map((task) => `<li class="task-row ${completed ? "completed" : ""}" data-task-id="${task.id}">
      <button class="task-check" type="button" data-task-action="${completed ? "undo" : "complete"}" aria-label="${completed ? "Вернуть задачу" : "Завершить задачу"}">${completed ? "✓" : ""}</button>
      <div class="task-copy"><h3>${escapeHTML(task.title)}</h3>${taskMeta(task)}</div>
      ${completed ? '<button class="undo-button" type="button" data-task-action="undo">Вернуть</button>' : ""}
    </li>`).join("")}</ul>`;
  }

  function renderToday(data) {
    const stats = data.stats;
    const nextItems = data.timeline.filter((item) => parseDate(item.end || item.start) >= parseDate(data.now));
    const conflictNotice = data.conflicts.length ? `<div class="notice danger"><strong>${data.conflicts.length} ${plural(data.conflicts.length, "конфликт", "конфликта", "конфликтов")}</strong> в расписании. Проверь соседние события в планнере.</div>` : "";
    return `${conflictNotice}
      <div class="summary-strip" aria-label="Сводка дня">
        <div class="metric"><strong>${stats.events}</strong><span>${plural(stats.events, "событие", "события", "событий")}</span></div>
        <div class="metric"><strong>${stats.active_tasks}</strong><span>${plural(stats.active_tasks, "задача", "задачи", "задач")}</span></div>
        <div class="metric"><strong>${stats.overdue_tasks}</strong><span>просрочено</span></div>
      </div>
      <section class="section"><div class="section-heading"><h2>Дальше по плану</h2><span>${nextItems.length}</span></div>${timelineHTML(nextItems.slice(0, 8))}</section>
      <section class="section"><div class="section-heading"><h2>Главное</h2><span>до 5 задач</span></div>
        ${data.focus.length ? data.focus.map((task, index) => `<article class="focus-card"><span class="focus-number">${index + 1}</span><div><h3>${escapeHTML(task.title)}</h3>${taskMeta(task)}</div></article>`).join("") : '<div class="empty-state">Активных задач нет. Добавь одну кнопкой +.</div>'}
      </section>`;
  }

  function dayGroupsHTML(data) {
    const groups = new Map();
    data.timeline.forEach((item) => {
      const key = dateKey(item.start);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    });
    if (!groups.size) return '<div class="empty-state">На выбранный период событий пока нет.</div>';
    return [...groups.entries()].map(([key, items]) => `<section class="day-group">
      <div class="day-heading"><time datetime="${key}">${formatDate(items[0].start, { weekday: "long", day: "numeric", month: "long" })}</time><span>${items.length}</span></div>
      ${timelineHTML(items)}
    </section>`).join("");
  }

  function periodControlHTML() {
    return `<div class="segment-control" role="tablist" aria-label="Период расписания">
      ${Object.entries(periods).map(([key, label]) => `<button type="button" role="tab" data-period="${key}" class="${state.period === key ? "active" : ""}" aria-selected="${state.period === key}">${label}</button>`).join("")}
    </div>`;
  }

  function renderPlanner(data) {
    return `${periodControlHTML()}
      ${data.conflicts.length ? `<div class="notice danger"><strong>Найдено конфликтов: ${data.conflicts.length}.</strong> События пересекаются или между ними мало времени на дорогу.</div>` : ""}
      ${dayGroupsHTML(data)}`;
  }

  function renderTasks(data) {
    const active = data.tasks.filter((task) => task.status === "active");
    const ready = active.filter((task) => task.planning_state !== "inbox");
    const inbox = active.filter((task) => task.planning_state === "inbox");
    const completed = data.tasks.filter((task) => task.status === "completed");
    return `<section class="section"><div class="section-heading"><h2>Активные</h2><span>${ready.length}</span></div>${taskRowsHTML(ready)}</section>
      ${inbox.length ? `<section class="section"><div class="section-heading"><h2>Нужно уточнить</h2><span>${inbox.length}</span></div>${taskRowsHTML(inbox)}</section>` : ""}
      ${completed.length ? `<section class="section"><div class="section-heading"><h2>Завершено сегодня</h2><span>${completed.length}</span></div>${taskRowsHTML(completed, true)}</section>` : ""}`;
  }

  function renderProfile(profile) {
    const options = profile.timezone_options.map((item) => `<option value="${escapeHTML(item.timezone)}" ${item.timezone === profile.timezone ? "selected" : ""}>${escapeHTML(item.label)} · ${escapeHTML(item.offset)}</option>`).join("");
    const sync = profile.hse.synced_at ? `Последнее обновление: ${formatDate(profile.hse.synced_at, { day: "numeric", month: "short" })}, ${formatTime(profile.hse.synced_at)}` : "Файл ещё не загружен";
    const bridgeStatus = profile.hse.iphone_bridge
      ? "Можно подключить автоматическое обновление с iPhone"
      : "Синхронизация с iPhone ещё не настроена";
    return `<section class="profile-panel"><div class="panel-heading"><h2>${escapeHTML(profile.name)}</h2><p>Время и дорога учитываются в расписании.</p></div>
      <form class="profile-fields" id="profile-form">
        <label class="field"><span>Город и часовой пояс</span><select name="timezone">${options}</select></label>
        <label class="field"><span>Откуда обычно начинаешь день</span><input name="home" value="${escapeHTML(profile.home)}" maxlength="255" placeholder="Дом, район или адрес"></label>
        <label class="field"><span>Фокус в день, минут</span><input name="daily_focus_minutes" type="number" min="60" max="480" step="15" value="${profile.planning.daily_focus_minutes}"></label>
        <button class="primary-button full-width" type="submit">Сохранить профиль</button>
      </form></section>
      <section class="profile-panel"><div class="panel-heading"><h2>Календарь HSE с iPhone</h2><p>${profile.hse.events} ${plural(profile.hse.events, "событие", "события", "событий")} · ${escapeHTML(sync)}</p></div>
        <div class="profile-fields">
          <div class="source-flow"><span>HSE App</span><b>→</b><span>Календарь HSE</span><b>→</b><span>Планнер</span></div>
          <p class="helper">${escapeHTML(bridgeStatus)}. Личные календари не отправляются.</p>
          ${profile.hse.iphone_bridge ? '<button class="primary-button full-width" id="hse-setup-button" type="button">Настроить автоматизацию</button>' : '<div class="notice">Серверный приёмник календаря пока выключен.</div>'}
          <div id="hse-shortcut-setup" class="shortcut-setup hidden"></div>
          <details class="fallback-import">
            <summary>Если есть файл .ics</summary>
            <form id="hse-form">
              <label class="field file-input"><span>Файл календаря .ics</span><input name="calendar" type="file" accept=".ics,text/calendar" required></label>
              <p class="helper">Файл заменит только прежние события HSE. Задачи и другие календари останутся на месте.</p>
              <button class="primary-button full-width" type="submit">Загрузить .ics</button>
            </form>
          </details>
        </div></section>`;
  }

  function shortcutSetupHTML(config) {
    return `<div class="setup-heading"><h3>Автоматизация после HSE App</h3><p class="helper">Создаётся один раз в приложении «Команды».</p></div>
      <ol class="setup-steps">
        <li><strong>Автоматизация → Приложение</strong><span>Выбери HSE App, условие «Закрыто», запуск немедленно.</span></li>
        <li><strong>Найти события календаря</strong><span>Календарь — ${escapeHTML(config.calendar)}, даты — ближайшие ${config.window_days} дней.</span></li>
        <li><strong>Повторить для каждого события</strong><span>Собери словарь с полями id, title, calendar, location и notes; даты start и end отформатируй как ISO 8601.</span></li>
        <li><strong>Добавить в переменную Events</strong><span>Размести сразу после словаря, внутри повтора. Так все словари сохранятся в одном списке.</span></li>
        <li><strong>Получить содержимое URL</strong><span>После «Конец повтора»: POST, тело JSON. token — секрет ниже; events — переменная Events с типом «Массив». Заголовки не нужны.</span></li>
      </ol>
      <label class="field compact-field"><span>URL</span><div class="copy-row"><input id="shortcut-endpoint" readonly value="${escapeHTML(config.endpoint)}"><button class="secondary-button" type="button" data-copy-input="shortcut-endpoint">Копировать</button></div></label>
      <label class="field compact-field"><span>Значение поля token</span><div class="copy-row"><input id="shortcut-token" type="password" readonly value="${escapeHTML(config.token)}"><button class="secondary-button" type="button" data-copy-input="shortcut-token">Копировать</button></div></label>
      <p class="secret-warning">Токен даёт доступ только к загрузке календаря HSE. Не отправляй его в чат, заголовок или ссылку.</p>`;
  }

  function resourceStatusHTML(entry) {
    if (entry.error) {
      const message = entry.error.status === 401
        ? "Открой Mini App кнопкой «Управление» в чате с ботом."
        : entry.error.message;
      return `<section class="status-screen section-status" aria-live="polite">
        <span class="status-symbol" aria-hidden="true">!</span>
        <h2>Раздел временно недоступен</h2>
        <p>${escapeHTML(message)}</p>
        <button class="primary-button" data-retry-current type="button">Повторить</button>
      </section>`;
    }
    return `<section class="status-screen section-status" aria-live="polite">
      <span class="loader" aria-hidden="true"></span>
      <p>Открываю только этот раздел…</p>
    </section>`;
  }

  function render() {
    const key = currentResourceKey();
    const entry = resources.get(key);
    els.title.textContent = titles[state.view];
    document.querySelectorAll(".nav-item").forEach((button) => {
      const active = button.dataset.view === state.view;
      button.classList.toggle("active", active);
      button.setAttribute("aria-current", active ? "page" : "false");
    });
    els.add.classList.toggle("hidden", state.view === "profile");
    els.refresh.classList.toggle("spinning", entry.loading);
    els.loading.classList.add("hidden");
    els.error.classList.add("hidden");
    els.view.classList.remove("hidden");
    if (state.view === "profile") {
      els.date.textContent = "Настройки";
      if (entry.data === null) {
        els.view.innerHTML = resourceStatusHTML(entry);
      } else {
        state.timezone = entry.data.timezone;
        els.view.innerHTML = renderProfile(entry.data);
      }
    } else {
      if (entry.data === null) {
        els.view.innerHTML = `${state.view === "planner" ? periodControlHTML() : ""}${resourceStatusHTML(entry)}`;
      } else {
        const schedule = entry.data;
        state.timezone = schedule.user.timezone;
        els.date.textContent = formatDate(schedule.now, { weekday: "long", day: "numeric", month: "long" });
        if (state.view === "planner") els.view.innerHTML = renderPlanner(schedule);
        else if (state.view === "tasks") els.view.innerHTML = renderTasks(schedule);
        else els.view.innerHTML = renderToday(schedule);
      }
    }
    bindViewActions();
  }
  function toast(message) {
    els.toast.textContent = message;
    els.toast.classList.add("visible");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => els.toast.classList.remove("visible"), 2400);
  }

  function loadSchedule(period, force = false) {
    return resources.load(scheduleKey(period), force);
  }
  function loadProfile(force = false) {
    return resources.load("profile", force);
  }
  function loadCurrent(force = false) {
    return resources.load(currentResourceKey(), force);
  }
  function loadCurrentInBackground(force = false) {
    loadCurrent(force).catch(() => {});
  }

  function bindViewActions() {
    document.querySelectorAll("[data-period]").forEach((button) => button.addEventListener("click", () => {
      state.period = button.dataset.period;
      render();
      loadCurrentInBackground();
    }));
    document.querySelector("[data-retry-current]")?.addEventListener(
      "click",
      () => loadCurrentInBackground(true),
    );
    document.querySelectorAll("[data-task-action]").forEach((button) => button.addEventListener("click", async () => {
      const row = button.closest("[data-task-id]");
      const complete = button.dataset.taskAction === "complete";
      button.disabled = true;
      try {
        await api(`api/v1/tasks/${row.dataset.taskId}`, { method: "PATCH", body: JSON.stringify({ status: complete ? "completed" : "active" }) });
        resources.invalidatePrefix("schedule:");
        await loadSchedule("today", true);
        tg?.HapticFeedback?.notificationOccurred("success");
        toast(complete ? "Готово. Задача осталась в завершённых." : "Задача возвращена");
      } catch (error) { toast(error.message); button.disabled = false; }
    }));
    document.querySelector("#profile-form")?.addEventListener("submit", saveProfile);
    document.querySelector("#hse-form")?.addEventListener("submit", importHSE);
    document.querySelector("#hse-setup-button")?.addEventListener("click", showShortcutSetup);
  }

  async function showShortcutSetup(event) {
    const button = event.currentTarget;
    const target = document.querySelector("#hse-shortcut-setup");
    button.disabled = true;
    button.textContent = "Загружаю…";
    try {
      if (!state.shortcutConfig) {
        state.shortcutConfig = await api("api/v1/hse/shortcut-config");
      }
      target.innerHTML = shortcutSetupHTML(state.shortcutConfig);
      target.classList.remove("hidden");
      button.classList.add("hidden");
      target.querySelectorAll("[data-copy-input]").forEach((copyButton) => {
        copyButton.addEventListener("click", () => copyInput(copyButton.dataset.copyInput));
      });
    } catch (error) {
      toast(error.message);
      button.disabled = false;
      button.textContent = "Настроить автоматизацию";
    }
  }

  async function copyInput(id) {
    const input = document.getElementById(id);
    try {
      await navigator.clipboard.writeText(input.value);
    } catch (_error) {
      input.type = "text";
      input.select();
      document.execCommand("copy");
      input.type = id === "shortcut-token" ? "password" : "text";
    }
    tg?.HapticFeedback?.selectionChanged();
    toast("Скопировано");
  }

  async function saveProfile(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    const data = new FormData(form);
    try {
      const profile = await api("api/v1/profile", { method: "PATCH", body: JSON.stringify({
        timezone: data.get("timezone"), home: data.get("home"), daily_focus_minutes: Number(data.get("daily_focus_minutes")),
      }) });
      resources.set("profile", profile);
      resources.invalidatePrefix("schedule:");
      toast("Профиль сохранён"); tg?.HapticFeedback?.notificationOccurred("success");
    } catch (error) { toast(error.message); button.disabled = false; }
  }

  async function importHSE(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button[type=submit]");
    const file = new FormData(form).get("calendar");
    if (!(file instanceof File) || !file.size) return;
    button.disabled = true; button.textContent = "Загружаю…";
    try {
      const result = await api("api/v1/hse/import", { method: "POST", body: file, headers: { "Content-Type": "text/calendar" } });
      resources.invalidate("profile");
      resources.invalidatePrefix("schedule:");
      await loadProfile(true);
      toast(`Загружено событий: ${result.imported}`); tg?.HapticFeedback?.notificationOccurred("success");
    } catch (error) { toast(error.message); button.disabled = false; button.textContent = "Загрузить .ics"; }
  }

  document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => {
    state.view = button.dataset.view;
    render();
    loadCurrentInBackground();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }));
  els.refresh.addEventListener("click", () => loadCurrentInBackground(true));
  els.retry.addEventListener("click", () => loadCurrentInBackground(true));
  els.add.addEventListener("click", () => { els.form.reset(); document.querySelector("#task-duration").value = 30; els.dialog.showModal(); setTimeout(() => document.querySelector("#task-title").focus(), 80); });
  els.form.addEventListener("submit", async (event) => {
    if (event.submitter?.value === "cancel") return;
    event.preventDefault();
    if (!els.form.reportValidity()) return;
    const button = document.querySelector("#save-task-button");
    const data = new FormData(els.form);
    button.disabled = true;
    try {
      await api("api/v1/tasks", { method: "POST", body: JSON.stringify({ title: data.get("title"), deadline: data.get("deadline") || null, estimated_minutes: Number(data.get("estimated_minutes")) }) });
      resources.invalidatePrefix("schedule:");
      await loadSchedule(state.view === "planner" ? state.period : "today", true);
      els.dialog.close(); toast("Задача добавлена"); tg?.HapticFeedback?.notificationOccurred("success");
    } catch (error) { toast(error.message); }
    finally { button.disabled = false; }
  });
  els.dialog.addEventListener("click", (event) => {
    if (event.target === els.dialog) els.dialog.close();
  });

  initializeTelegram();
  els.date.textContent = new Intl.DateTimeFormat("ru-RU", { weekday: "long", day: "numeric", month: "long" }).format(new Date());
  render();
  loadCurrentInBackground(true);
})();
