(() => {
  "use strict";

  const tg = window.Telegram?.WebApp;
  const state = {
    view: "today",
    period: "today",
    shortcutConfig: null,
    healthShortcutConfig: null,
    timezone: undefined,
  };
  const titles = {
    today: "Сегодня",
    planner: "Планнер",
    tasks: "Задачи",
    health: "Здоровье",
    profile: "Профиль",
  };
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
    if (state.view === "health") return "health";
    return scheduleKey(state.view === "planner" ? state.period : "today");
  }
  function loadResource(key) {
    if (key === "profile") return api("api/v1/profile");
    if (key === "health") return api("api/v1/health");
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

  function commandItemHTML(label, item, emptyText) {
    if (!item) {
      return `<article class="command-card"><span>${escapeHTML(label)}</span><strong>${escapeHTML(emptyText)}</strong><p>Можно оставить время свободным или добавить важную задачу.</p></article>`;
    }
    const when = item.start ? formatTime(item.start) : "";
    const meta = [when, item.meta, item.location].filter(Boolean).join(" · ");
    return `<article class="command-card ${escapeHTML(item.kind || "")}"><span>${escapeHTML(label)}</span><strong>${escapeHTML(item.title)}</strong>${meta ? `<p>${escapeHTML(meta)}</p>` : ""}</article>`;
  }

  function renderToday(data) {
    const stats = data.stats;
    const assistant = data.assistant || {};
    const nextItems = data.timeline.filter((item) => parseDate(item.end || item.start) >= parseDate(data.now));
    const risks = (assistant.risks || []).map((risk) => `<article class="risk-row ${escapeHTML(risk.level || "warning")}"><div><strong>${escapeHTML(risk.title)}</strong><p>${escapeHTML(risk.text)}</p></div></article>`).join("");
    const resource = assistant.resource || {};
    const resourceFacts = resource.connected
      ? [resource.sleep_minutes ? sleepValue(Number(resource.sleep_minutes)) : "сон — нет данных", resource.steps ? `${Number(resource.steps).toLocaleString("ru-RU")} шагов` : "шаги — нет данных"].join(" · ")
      : "Подключение здоровья не блокирует остальные функции";
    return `<section class="command-center">
        <div class="command-grid">
          ${commandItemHTML("Сейчас", assistant.now, "Срочных действий нет")}
          ${commandItemHTML("Дальше", assistant.next, "Расписание свободно")}
        </div>
        <article class="resource-row ${escapeHTML(resource.level || "unknown")}"><span>Ресурс</span><div><strong>${escapeHTML(resource.title || "Нет данных о восстановлении")}</strong><p>${escapeHTML(resourceFacts)}</p></div></article>
      </section>
      ${risks ? `<section class="section"><div class="section-heading"><h2>Риски</h2><span>${assistant.risks.length}</span></div><div class="risk-list">${risks}</div></section>` : ""}
      <div class="summary-strip" aria-label="Сводка дня">
        <div class="metric"><strong>${stats.events}</strong><span>${plural(stats.events, "событие", "события", "событий")}</span></div>
        <div class="metric"><strong>${stats.active_tasks}</strong><span>${plural(stats.active_tasks, "задача", "задачи", "задач")}</span></div>
        <div class="metric"><strong>${stats.overdue_tasks}</strong><span>просрочено</span></div>
      </div>
      <section class="section"><div class="section-heading"><h2>Расписание</h2><span>${nextItems.length}</span></div>${timelineHTML(nextItems.slice(0, 8))}</section>
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
    if (!groups.size) {
      const nextHSE = data.upcoming?.next_hse_event;
      const incomplete = data.sources?.hse?.status === "incomplete";
      const nextHint = nextHSE
        ? `<span>${incomplete ? "Из неполного снимка известно событие" : "Ближайшее событие HSE"} — ${formatDate(nextHSE.start, { weekday: "long", day: "numeric", month: "long" })}, ${formatTime(nextHSE.start)}.</span>`
        : "";
      return `<div class="empty-state"><strong>На выбранный период событий нет.</strong>${nextHint}</div>`;
    }
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
    const sourceWarning = data.sources?.hse?.status === "incomplete"
      ? `<div class="notice danger"><strong>Расписание HSE неполное.</strong> ${escapeHTML(data.sources.hse.message || "Последние известные данные сохранены до следующей проверки.")}</div>`
      : "";
    return `${periodControlHTML()}
      ${sourceWarning}
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
    const syncStatus = profile.hse.sync_status || (profile.hse.synced_at ? "fresh" : "never");
    const syncLabel = syncStatus === "fresh"
      ? "Автоматизация работает"
      : syncStatus === "incomplete"
        ? "Источник прислал неполные данные"
      : syncStatus === "stale"
        ? "Давно не обновлялось"
        : "Ещё не запускалась";
    const syncDetail = syncStatus === "incomplete"
      ? (profile.hse.integrity_message || `Получено событий: ${profile.hse.received_count || profile.hse.events}. Последние известные данные сохранены.`)
      : profile.hse.synced_at
      ? `${profile.hse.last_result === "unchanged" ? "Проверено без изменений" : "Обновлено"}: ${formatDate(profile.hse.synced_at, { day: "numeric", month: "short" })}, ${formatTime(profile.hse.synced_at)}`
      : "После первого фонового запуска здесь появится время проверки.";
    const bridgeStatus = profile.hse.iphone_bridge
      ? "В планнер попадает только календарь HSE; личные события остаются на iPhone."
      : "Серверный приёмник календаря пока выключен.";
    const nextHSE = profile.hse.first_start
      ? `Ближайшее — ${formatDate(profile.hse.first_start, { weekday: "long", day: "numeric", month: "long" })}, ${formatTime(profile.hse.first_start)}`
      : "В загруженном диапазоне будущих занятий нет.";
    const focusWarning = profile.planning.focus_warning
      ? `<div class="notice ${profile.planning.focus_load_level === "overload" ? "danger" : ""}"><strong>Высокая нагрузка.</strong> ${escapeHTML(profile.planning.focus_warning)}</div>`
      : "";
    return `<section class="profile-panel"><div class="panel-heading"><h2>${escapeHTML(profile.name)}</h2><p>Время и дорога учитываются в расписании.</p></div>
      <form class="profile-fields" id="profile-form">
        <label class="field"><span>Город и часовой пояс</span><select name="timezone">${options}</select></label>
        <label class="field"><span>Стартовая точка для маршрутов (необязательно)</span><input name="home" value="${escapeHTML(profile.home)}" maxlength="255" placeholder="Район, метро или ориентир"></label>
        <p class="helper">Точный адрес не нужен. Это только ориентир для примерного времени в дороге; помощник не проверяет существование адреса.</p>
        <label class="field"><span>Желаемый фокус в день, минут</span><input name="daily_focus_minutes" type="number" min="60" max="1440" step="15" value="${profile.planning.daily_focus_minutes}"></label>
        <p class="helper">Можно указать до 24 часов. Безопасный автоплан всё равно поставит не больше ${Math.round(profile.planning.auto_planning_minutes / 60)} ч фокуса и оставит время вне задач.</p>
        ${focusWarning}
        <button class="primary-button full-width" type="submit">Сохранить профиль</button>
      </form></section>
      <section class="profile-panel"><div class="panel-heading"><h2>Подключения · календарь HSE</h2><p>${profile.hse.events} ${plural(profile.hse.events, "событие", "события", "событий")} в рабочем расписании · ${escapeHTML(nextHSE)}</p></div>
        <div class="profile-fields">
          <div class="sync-state-card ${syncStatus}"><span class="sync-state-dot" aria-hidden="true"></span><div><strong>${escapeHTML(syncLabel)}</strong><span>${escapeHTML(syncDetail)}</span></div></div>
          <div class="source-flow"><span>HSE App</span><b>→</b><span>Календарь HSE</span><b>→</b><span>Планнер</span></div>
          <p class="helper">${escapeHTML(bridgeStatus)}</p>
          ${profile.hse.iphone_bridge ? `<button class="primary-button full-width" id="hse-setup-button" type="button">${syncStatus === "fresh" ? "Управление подключением" : syncStatus === "incomplete" ? "Исправить подключение" : "Настроить подключение"}</button>` : '<div class="notice">Серверный приёмник календаря пока выключен.</div>'}
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
    return `<div class="setup-heading"><h3>Полностью фоновая работа</h3><p class="helper">Команда уже готова. Осталось один раз настроить два запуска в приложении «Команды».</p></div>
      <ol class="setup-steps">
        <li><strong>Убери отладочный просмотр</strong><span>Удали действия «Показать» или «Быстрый просмотр» после запроса. «Получить содержимое URL» должно быть последним действием команды.</span></li>
        <li><strong>Запуск после HSE App</strong><span>Автоматизация → Приложение → HSE App → «Закрыто» → запустить эту команду → «Немедленно». Отключи уведомление о запуске.</span></li>
        <li><strong>Ежедневная страховка в ${escapeHTML(config.daily_trigger || "07:00")}</strong><span>Автоматизация → Время суток → запустить эту же команду → «Немедленно». Она повторно проверит ближайшие ${config.window_days} дней.</span></li>
        <li><strong>Готово</strong><span>Одинаковое расписание больше не перезаписывается. В профиле обновится время проверки, а изменения сразу попадут в «Сегодня» и «Планнер».</span></li>
      </ol>
      <details class="fallback-import"><summary>Данные команды для восстановления</summary>
        <label class="field compact-field"><span>URL</span><div class="copy-row"><input id="shortcut-endpoint" readonly value="${escapeHTML(config.endpoint)}"><button class="secondary-button" type="button" data-copy-input="shortcut-endpoint">Копировать</button></div></label>
        <label class="field compact-field"><span>Значение поля token</span><div class="copy-row"><input id="shortcut-token" type="password" readonly value="${escapeHTML(config.token)}"><button class="secondary-button" type="button" data-copy-input="shortcut-token">Копировать</button></div></label>
        <p class="secret-warning">Токен даёт доступ только к загрузке календаря HSE. Не отправляй его в чат или ссылку.</p>
      </details>`;
  }

  function healthMetricHTML(label, value, hint, progress = null) {
    const bar = progress === null
      ? ""
      : `<span class="health-progress" aria-hidden="true"><i style="width:${Math.max(0, Math.min(100, progress))}%"></i></span>`;
    return `<article class="health-metric"><span>${escapeHTML(label)}</span><strong>${escapeHTML(value)}</strong><small>${escapeHTML(hint)}</small>${bar}</article>`;
  }

  function sleepValue(minutes) {
    if (!minutes) return "—";
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    return rest ? `${hours} ч ${rest} мин` : `${hours} ч`;
  }

  function workoutHTML(workout, history = false) {
    const statusLabels = {
      planned: "запланирована",
      in_progress: "идёт сейчас",
      completed: "выполнена",
      skipped: "пропущена",
    };
    const when = history && workout.completed_at ? workout.completed_at : workout.scheduled_for;
    const exerciseRows = (workout.exercises || []).map((exercise) => {
      const prescription = [
        exercise.sets ? `${exercise.sets} подх.` : "",
        exercise.reps ? `${exercise.reps} повт.` : "",
        exercise.suggested_weight_kg ? `${exercise.suggested_weight_kg} кг` : "",
      ].filter(Boolean).join(" · ");
      return `<li><span>${escapeHTML(exercise.name)}</span>${prescription ? `<small>${escapeHTML(prescription)}</small>` : ""}</li>`;
    }).join("");
    return `<article class="workout-card">
      <div class="workout-heading"><div><strong>${escapeHTML(workout.title)}</strong><span>${formatDate(when, { weekday: "short", day: "numeric", month: "short" })}, ${formatTime(when)}</span></div><b>${escapeHTML(statusLabels[workout.status] || workout.status)}</b></div>
      ${workout.focus ? `<p>${escapeHTML(workout.focus)}</p>` : ""}
      ${exerciseRows ? `<details><summary>Упражнения</summary><ul class="exercise-list">${exerciseRows}</ul></details>` : ""}
      ${workout.rpe ? `<p class="workout-result">Нагрузка RPE ${escapeHTML(workout.rpe)}/10${workout.notes ? ` · ${escapeHTML(workout.notes)}` : ""}</p>` : ""}
    </article>`;
  }

  function renderHealth(data) {
    const snapshot = data.apple_health.snapshot;
    const recovery = data.recovery;
    const recommendations = (recovery.recommendations || [])
      .map((item) => `<li>${escapeHTML(item)}</li>`).join("");
    const sleep = snapshot ? Number(snapshot.sleep_minutes || 0) : 0;
    const steps = snapshot ? Number(snapshot.steps || 0) : 0;
    const stepGoal = snapshot ? Number(snapshot.step_goal || 0) : 0;
    const workoutMinutes = snapshot ? Number(snapshot.workout_minutes || 0) : 0;
    const stepProgress = stepGoal ? Math.round((steps / stepGoal) * 100) : null;
    const waterGoal = Number(data.goals.water_ml || 0);
    const proteinGoal = Number(data.goals.protein_g || 0);
    const water = Number(data.intake.water_ml || 0);
    const protein = Number(data.intake.protein_g || 0);
    const upcoming = data.training.upcoming || [];
    const history = data.training.history || [];
    const healthStatus = snapshot
      ? `${snapshot.fresh ? "Сегодня" : "Последние данные"} · ${formatDate(snapshot.captured_at, { day: "numeric", month: "short" })}, ${formatTime(snapshot.captured_at)}`
      : "Apple Health ещё не подключён";
    const setup = data.apple_health.bridge_enabled
      ? `<button class="primary-button full-width" id="health-setup-button" type="button">Подключить Apple Health</button><div id="health-shortcut-setup" class="shortcut-setup hidden"></div>`
      : '<div class="notice">Серверный приёмник Apple Health пока выключен.</div>';
    return `<section class="recovery-card ${escapeHTML(recovery.level)}">
        <span class="recovery-label">Баланс дня</span>
        <h2>${escapeHTML(recovery.title)}</h2>
        <p>${escapeHTML(recovery.message)}</p>
        ${recommendations ? `<ul>${recommendations}</ul>` : ""}
      </section>
      <section class="section"><div class="section-heading"><h2>Сон и движение</h2><span>${escapeHTML(healthStatus)}</span></div>
        <div class="health-grid">
          ${healthMetricHTML("Сон", sleepValue(sleep), sleep ? "последняя ночь" : "нет данных")}
          ${healthMetricHTML("Шаги", steps ? steps.toLocaleString("ru-RU") : "—", stepGoal ? `цель ${stepGoal.toLocaleString("ru-RU")}` : "нет данных", stepProgress)}
          ${healthMetricHTML("Активность", workoutMinutes ? `${workoutMinutes} мин` : "—", snapshot?.active_energy_kcal ? `${snapshot.active_energy_kcal} ккал` : "нет данных")}
        </div>
        <div class="profile-panel health-connect"><div class="profile-fields"><p class="helper">Данные обновляются отдельно от календаря и задач: пустой Apple Health не блокирует остальные разделы.</p>${setup}</div></div>
      </section>
      <section class="section"><div class="section-heading"><h2>Сегодня</h2><span>быстрый учёт</span></div>
        <div class="intake-grid">
          <article class="intake-card"><span>Вода</span><strong>${water} мл</strong><small>${waterGoal ? `цель ${waterGoal} мл` : "личная цель не задана"}</small><span class="health-progress"><i style="width:${waterGoal ? Math.min(100, Math.round((water / waterGoal) * 100)) : 0}%"></i></span><div class="counter-actions"><button type="button" data-intake-field="water_ml" data-intake-delta="-250">−250</button><button type="button" data-intake-field="water_ml" data-intake-delta="250">+250</button></div></article>
          <article class="intake-card"><span>Белок</span><strong>${protein} г</strong><small>${proteinGoal ? `цель ${proteinGoal} г` : "личная цель не задана"}</small><span class="health-progress"><i style="width:${proteinGoal ? Math.min(100, Math.round((protein / proteinGoal) * 100)) : 0}%"></i></span><div class="counter-actions"><button type="button" data-intake-field="protein_g" data-intake-delta="-20">−20</button><button type="button" data-intake-field="protein_g" data-intake-delta="20">+20</button></div></article>
        </div>
        <div class="nutrition-summary"><span>КБЖУ</span><strong>${Number(data.intake.calories_kcal || 0)} ккал</strong><small>Б ${protein} · Ж ${Number(data.intake.fat_g || 0)} · У ${Number(data.intake.carbs_g || 0)} г</small></div>
        <details class="health-goals"><summary>Личные цели воды и белка</summary><form id="health-goals-form"><div class="form-row"><label class="field"><span>Вода, мл</span><input name="water_ml" type="number" min="0" max="10000" step="250" value="${waterGoal}"></label><label class="field"><span>Белок, г</span><input name="protein_g" type="number" min="0" max="400" step="5" value="${proteinGoal}"></label></div><button class="primary-button full-width" type="submit">Сохранить цели</button></form></details>
      </section>
      <section class="section"><div class="section-heading"><h2>Тренировки</h2><span>${upcoming.length} впереди</span></div>
        <article class="micro-plan ${escapeHTML(data.training.micro_plan.level)}"><span>Следующий шаг</span><strong>${escapeHTML(data.training.micro_plan.title)}</strong><p>${escapeHTML(data.training.micro_plan.text)}</p></article>
        ${upcoming.length ? `<div class="workout-list">${upcoming.map((item) => workoutHTML(item)).join("")}</div>` : '<div class="empty-state">Ближайших тренировок нет. Попроси бота составить план тренировок.</div>'}
        ${history.length ? `<details class="training-history"><summary>История тренировок</summary><div class="workout-list">${history.map((item) => workoutHTML(item, true)).join("")}</div></details>` : ""}
      </section>
      <p class="medical-disclaimer">${escapeHTML(data.medical_disclaimer)}</p>`;
  }

  function healthShortcutSetupHTML(config) {
    const fields = (config.fields || []).map((field) => `<code>${escapeHTML(field)}</code>`).join(", ");
    return `<div class="setup-heading"><h3>Одна команда, два фоновых запуска</h3><p class="helper">Команда отправляет один словарь, поэтому циклы и массивы не нужны.</p></div>
      <ol class="setup-steps">
        <li><strong>Создай словарь «Снимок здоровья»</strong><span>Добавь поля ${fields}. Значения возьми действиями «Найти образцы здоровья» и «Получить сведения об образцах» за сегодня; сон — за прошедшую ночь.</span></li>
        <li><strong>Добавь token в тот же словарь</strong><span>Скопируй значение ниже. Поле date передай как текущую дату в формате ГГГГ-ММ-ДД.</span></li>
        <li><strong>Отправь словарь</strong><span>«Получить содержимое URL» → POST → JSON. В качестве тела передай сам словарь, без массива и без повтора.</span></li>
        <li><strong>Автоматизируй</strong><span>Создай два запуска по времени: ${escapeHTML(config.morning_trigger)} и ${escapeHTML(config.evening_trigger)}. Для обоих выбери «Немедленно» и отключи вопрос перед запуском.</span></li>
      </ol>
      <details class="fallback-import"><summary>URL и секрет команды</summary>
        <label class="field compact-field"><span>URL</span><div class="copy-row"><input id="health-shortcut-endpoint" readonly value="${escapeHTML(config.endpoint)}"><button class="secondary-button" type="button" data-copy-input="health-shortcut-endpoint">Копировать</button></div></label>
        <label class="field compact-field"><span>Значение поля token</span><div class="copy-row"><input id="health-shortcut-token" type="password" readonly value="${escapeHTML(config.token)}"><button class="secondary-button" type="button" data-copy-input="health-shortcut-token">Копировать</button></div></label>
        <p class="secret-warning">Не отправляй токен в чат и не добавляй его в ссылку.</p>
      </details>`;
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
    els.add.classList.toggle("hidden", ["health", "profile"].includes(state.view));
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
    } else if (state.view === "health") {
      els.date.textContent = "Сон · движение · тренировки";
      if (entry.data === null) {
        els.view.innerHTML = resourceStatusHTML(entry);
      } else {
        state.timezone = entry.data.timezone;
        els.view.innerHTML = renderHealth(entry.data);
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
      loadCurrentInBackground(true);
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
    document.querySelector("#health-setup-button")?.addEventListener("click", showHealthShortcutSetup);
    document.querySelector("#health-goals-form")?.addEventListener("submit", saveHealthGoals);
    document.querySelectorAll("[data-intake-field]").forEach((button) => button.addEventListener("click", updateIntake));
  }

  async function showHealthShortcutSetup(event) {
    const button = event.currentTarget;
    const target = document.querySelector("#health-shortcut-setup");
    button.disabled = true;
    button.textContent = "Загружаю…";
    try {
      if (!state.healthShortcutConfig) {
        state.healthShortcutConfig = await api("api/v1/health/shortcut-config");
      }
      target.innerHTML = healthShortcutSetupHTML(state.healthShortcutConfig);
      target.classList.remove("hidden");
      button.classList.add("hidden");
      target.querySelectorAll("[data-copy-input]").forEach((copyButton) => {
        copyButton.addEventListener("click", () => copyInput(copyButton.dataset.copyInput));
      });
    } catch (error) {
      toast(error.message);
      button.disabled = false;
      button.textContent = "Подключить Apple Health";
    }
  }

  async function updateIntake(event) {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const payload = { [button.dataset.intakeField]: Number(button.dataset.intakeDelta) };
      const health = await api("api/v1/health/intake", { method: "POST", body: JSON.stringify(payload) });
      resources.set("health", health);
      tg?.HapticFeedback?.selectionChanged();
    } catch (error) {
      toast(error.message);
      button.disabled = false;
    }
  }

  async function saveHealthGoals(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button[type=submit]");
    const data = new FormData(form);
    button.disabled = true;
    try {
      const health = await api("api/v1/health/goals", { method: "PATCH", body: JSON.stringify({
        water_ml: Number(data.get("water_ml")),
        protein_g: Number(data.get("protein_g")),
      }) });
      resources.set("health", health);
      toast("Цели сохранены");
    } catch (error) {
      toast(error.message);
      button.disabled = false;
    }
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
      input.type = id.endsWith("shortcut-token") ? "password" : "text";
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
    loadCurrentInBackground(true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }));
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") loadCurrentInBackground(true);
  });
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
