import {
  createDateFormatters,
  escapeHTML,
  parseDate,
  plural,
  sleepValue,
} from "./formatters.mjs";
import { FAQ_ITEMS } from "./faq.mjs";

export function createViewRenderer({ getTimezone, periods }) {
  const { dateKey, formatDate, formatTime } = createDateFormatters(getTimezone);

  function itemDetails(item) {
    if (!item) return "";
    return [item.start ? formatTime(item.start) : "", item.meta, item.location]
      .filter(Boolean)
      .join(" · ");
  }

  function timelineHTML(items, { emptyText = "На это время ничего не запланировано." } = {}) {
    if (!items.length) {
      return `<div class="empty-state"><strong>Свободно</strong><span>${escapeHTML(emptyText)}</span></div>`;
    }
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
    if (task.deadline) {
      return `<span class="task-tag">до ${formatDate(task.deadline, { day: "numeric", month: "short" })}, ${formatTime(task.deadline)}</span>`;
    }
    if (task.planning_state === "inbox") return '<span class="task-tag">нужно уточнить</span>';
    return `<span class="task-tag">${task.estimated_minutes || 30} мин</span>`;
  }

  function taskRowsHTML(tasks, completed = false) {
    if (!tasks.length) {
      return '<div class="empty-state"><strong>Здесь пока пусто</strong><span>Добавь задачу одной кнопкой — остальное помощник разложит сам.</span><button class="text-button" type="button" data-open-task>Добавить задачу</button></div>';
    }
    return `<ul class="task-list">${tasks.map((task) => `<li class="task-row ${completed ? "completed" : ""}" data-task-id="${task.id}">
      <button class="task-check" type="button" data-task-action="${completed ? "undo" : "complete"}" aria-label="${completed ? "Вернуть задачу" : "Завершить задачу"}">${completed ? "✓" : ""}</button>
      <div class="task-copy"><h3>${escapeHTML(task.title)}</h3>${taskMeta(task)}</div>
      ${completed ? '<button class="undo-button" type="button" data-task-action="undo">Вернуть</button>' : ""}
    </li>`).join("")}</ul>`;
  }

  function currentFocusHTML(item) {
    if (!item) {
      return `<article class="focus-hero is-clear">
        <span class="hero-label">Сейчас</span>
        <h2>День под контролем</h2>
        <p>Срочных действий нет. Можно выбрать следующую задачу без спешки.</p>
        <button class="secondary-button" type="button" data-nav-view="tasks">Выбрать задачу</button>
      </article>`;
    }
    const details = itemDetails(item);
    const complete = item.task_id
      ? `<button class="secondary-button" type="button" data-task-action="complete">Готово</button>`
      : "";
    return `<article class="focus-hero ${escapeHTML(item.kind || "")}" ${item.task_id ? `data-task-id="${item.task_id}"` : ""}>
      <span class="hero-label">Сейчас</span>
      <h2>${escapeHTML(item.title)}</h2>
      ${details ? `<p>${escapeHTML(details)}</p>` : ""}
      ${complete}
    </article>`;
  }

  function renderToday(data) {
    const stats = data.stats || {};
    const assistant = data.assistant || {};
    const timeline = data.timeline || [];
    const nextItems = timeline.filter(
      (item) => parseDate(item.end || item.start) >= parseDate(data.now),
    );
    const risks = assistant.risks || [];
    const resource = assistant.resource || {};
    const resourceText = resource.connected
      ? [
        resource.sleep_minutes ? sleepValue(Number(resource.sleep_minutes)) : "сон — нет данных",
        resource.steps ? `${Number(resource.steps).toLocaleString("ru-RU")} шагов` : "шаги — нет данных",
      ].join(" · ")
      : "Здоровье не подключено";
    const next = assistant.next;
    const focus = (data.focus || []).slice(0, 4);

    return `<section class="today-intro">
        <p>${escapeHTML(data.user?.name || "Пользователь")}, вот реалистичный план на сегодня.</p>
        <div class="stat-pills" aria-label="Краткая сводка">
          <span><strong>${stats.events || 0}</strong> ${plural(stats.events || 0, "событие", "события", "событий")}</span>
          <span><strong>${stats.active_tasks || 0}</strong> ${plural(stats.active_tasks || 0, "задача", "задачи", "задач")}</span>
          ${(stats.overdue_tasks || 0) ? `<span class="is-warning"><strong>${stats.overdue_tasks}</strong> просрочено</span>` : ""}
        </div>
      </section>
      ${currentFocusHTML(assistant.now)}
      <div class="glance-grid">
        <article class="glance-card"><span>Дальше</span><strong>${escapeHTML(next?.title || "Расписание свободно")}</strong><p>${escapeHTML(next ? itemDetails(next) : "Оставь окно свободным или добавь важное дело")}</p></article>
        <button class="glance-card resource ${escapeHTML(resource.level || "unknown")}" type="button" data-nav-view="health"><span>Ресурс</span><strong>${escapeHTML(resource.title || "Нет данных")}</strong><p>${escapeHTML(resourceText)}</p></button>
      </div>
      ${risks.length ? `<section class="section attention-section"><div class="section-heading"><h2>Нужно внимание</h2><span>${risks.length}</span></div><div class="risk-list">${risks.map((risk) => `<article class="risk-row ${escapeHTML(risk.level || "warning")}"><strong>${escapeHTML(risk.title)}</strong><p>${escapeHTML(risk.text)}</p></article>`).join("")}</div></section>` : ""}
      <section class="section"><div class="section-heading"><h2>Ближайшее</h2><button class="text-button" type="button" data-nav-view="planner">Весь план</button></div>${timelineHTML(nextItems.slice(0, 5), { emptyText: "Сегодня можно оставить без жёсткого расписания." })}</section>
      <section class="section"><div class="section-heading"><h2>Главное на день</h2><button class="text-button" type="button" data-nav-view="tasks">Все задачи</button></div>
        ${focus.length ? focus.map((task, index) => `<article class="focus-card" data-task-id="${task.id}"><span class="focus-number">${index + 1}</span><div><h3>${escapeHTML(task.title)}</h3>${taskMeta(task)}</div><button class="task-check" type="button" data-task-action="complete" aria-label="Завершить задачу"></button></article>`).join("") : '<div class="empty-state"><strong>Главных задач нет</strong><span>Добавь первую — помощник найдёт ей место в расписании.</span><button class="text-button" type="button" data-open-task>Добавить задачу</button></div>'}
      </section>`;
  }

  function dayGroupsHTML(data) {
    const groups = new Map();
    (data.timeline || []).forEach((item) => {
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
      return `<div class="empty-state"><strong>На выбранный период событий нет</strong>${nextHint}<button class="text-button" type="button" data-open-task>Добавить задачу</button></div>`;
    }
    return [...groups.entries()].map(([key, items]) => `<section class="day-group">
      <div class="day-heading"><time datetime="${key}">${formatDate(items[0].start, { weekday: "long", day: "numeric", month: "long" })}</time><span>${items.length}</span></div>
      ${timelineHTML(items)}
    </section>`).join("");
  }

  function periodControlHTML(activePeriod) {
    return `<div class="segment-control" role="tablist" aria-label="Период расписания">
      ${Object.entries(periods).map(([key, label]) => `<button type="button" role="tab" data-period="${key}" class="${activePeriod === key ? "active" : ""}" aria-selected="${activePeriod === key}">${label}</button>`).join("")}
    </div>`;
  }

  function renderPlanner(data, activePeriod) {
    const sourceWarning = data.sources?.hse?.status === "incomplete"
      ? `<div class="notice danger"><strong>Расписание HSE неполное.</strong><span>${escapeHTML(data.sources.hse.message || "Последние надёжные данные сохранены до следующей проверки.")}</span></div>`
      : "";
    return `${periodControlHTML(activePeriod)}
      ${sourceWarning}
      ${(data.conflicts || []).length ? `<div class="notice danger"><strong>Конфликтов: ${data.conflicts.length}</strong><span>События пересекаются или между ними мало времени на дорогу.</span></div>` : ""}
      ${dayGroupsHTML(data)}`;
  }

  function renderTasks(data) {
    const active = (data.tasks || []).filter((task) => task.status === "active");
    const ready = active.filter((task) => task.planning_state !== "inbox");
    const inbox = active.filter((task) => task.planning_state === "inbox");
    const completed = (data.tasks || []).filter((task) => task.status === "completed");
    return `<section class="task-toolbar"><div><strong>${ready.length + inbox.length}</strong><span>в работе</span></div><button class="primary-button" type="button" data-open-task>Новая задача</button></section>
      <section class="section"><div class="section-heading"><h2>В плане</h2><span>${ready.length}</span></div>${taskRowsHTML(ready)}</section>
      ${inbox.length ? `<section class="section"><div class="section-heading"><h2>Нужно уточнить</h2><span>${inbox.length}</span></div>${taskRowsHTML(inbox)}</section>` : ""}
      ${completed.length ? `<details class="completed-group"><summary>Завершено сегодня · ${completed.length}</summary>${taskRowsHTML(completed, true)}</details>` : ""}`;
  }

  function renderProfile(profile) {
    const options = (profile.timezone_options || []).map(
      (item) => `<option value="${escapeHTML(item.timezone)}" ${item.timezone === profile.timezone ? "selected" : ""}>${escapeHTML(item.label)} · ${escapeHTML(item.offset)}</option>`,
    ).join("");
    const syncStatus = profile.hse.sync_status || (profile.hse.synced_at ? "fresh" : "never");
    const syncLabel = {
      fresh: "Календарь обновляется автоматически",
      incomplete: "Нужно исправить подключение",
      stale: "Давно не обновлялось",
      never: "Календарь ещё не подключён",
    }[syncStatus] || "Статус неизвестен";
    const syncDetail = syncStatus === "incomplete"
      ? (profile.hse.integrity_message || "Последние надёжные данные сохранены.")
      : profile.hse.synced_at
        ? `${profile.hse.last_result === "unchanged" ? "Проверено" : "Обновлено"}: ${formatDate(profile.hse.synced_at, { day: "numeric", month: "short" })}, ${formatTime(profile.hse.synced_at)}`
        : "Подключение выполняется один раз.";
    const focusWarning = profile.planning.focus_warning
      ? `<div class="notice ${profile.planning.focus_load_level === "overload" ? "danger" : ""}"><strong>Высокая нагрузка</strong><span>${escapeHTML(profile.planning.focus_warning)}</span></div>`
      : "";

    return `<section class="profile-card identity-card"><span class="avatar" aria-hidden="true">${escapeHTML((profile.name || "П").slice(0, 1).toUpperCase())}</span><div><h2>${escapeHTML(profile.name)}</h2><p>Автоплан включён: изменения задач и календаря учитываются без ручного обновления.</p></div></section>
      <section class="profile-card"><div class="panel-heading"><h2>Основное</h2><p>Только настройки, влияющие на ежедневный план.</p></div>
        <form class="profile-fields" id="profile-form">
          <label class="field"><span>Город и часовой пояс</span><select name="timezone">${options}</select></label>
          <details class="settings-disclosure"><summary>Тонкая настройка плана</summary>
            <label class="field"><span>Ориентир для маршрутов</span><input name="home" value="${escapeHTML(profile.home)}" maxlength="255" placeholder="Район или станция метро"></label>
            <label class="field"><span>Фокус в день, минут</span><input name="daily_focus_minutes" type="number" min="60" max="1440" step="15" value="${profile.planning.daily_focus_minutes}"></label>
            <p class="helper">Помощник ограничит автоплан безопасным объёмом: до ${Math.round(profile.planning.auto_planning_minutes / 60)} ч.</p>
            ${focusWarning}
          </details>
          <button class="primary-button full-width" type="submit">Сохранить</button>
        </form>
      </section>
      <section class="profile-card"><div class="panel-heading"><h2>Подключения</h2><p>После настройки источники работают в фоне.</p></div><div class="profile-fields">
        <article class="integration-row">
          <span class="integration-icon" aria-hidden="true">К</span>
          <div><strong>Календарь HSE</strong><p>${escapeHTML(syncLabel)}</p><small>${escapeHTML(syncDetail)}</small></div>
          ${profile.hse.iphone_bridge ? `<button class="secondary-button compact" type="button" data-connection="hse">${syncStatus === "fresh" ? "Данные" : "Настроить"}</button>` : '<span class="status-badge">выключено</span>'}
        </article>
        <button class="support-link" type="button" data-open-help><span>Справка и подключения</span><b aria-hidden="true">›</b></button>
        <details class="fallback-import"><summary>Резервный импорт .ics</summary>
          <form id="hse-form"><label class="field file-input"><span>Файл календаря</span><input name="calendar" type="file" accept=".ics,text/calendar" required></label><button class="primary-button full-width" type="submit">Загрузить .ics</button></form>
        </details>
      </div></section>`;
  }

  function healthMetricHTML(label, value, hint, progress = null) {
    const bar = progress === null
      ? ""
      : `<span class="health-progress" aria-hidden="true"><i style="width:${Math.max(0, Math.min(100, progress))}%"></i></span>`;
    return `<article class="health-metric"><span>${escapeHTML(label)}</span><strong>${escapeHTML(value)}</strong><small>${escapeHTML(hint)}</small>${bar}</article>`;
  }

  function workoutHTML(workout, history = false) {
    const labels = {
      planned: "запланирована",
      in_progress: "идёт сейчас",
      completed: "выполнена",
      skipped: "пропущена",
    };
    const when = history && workout.completed_at ? workout.completed_at : workout.scheduled_for;
    const exercises = (workout.exercises || []).map((exercise) => {
      const prescription = [
        exercise.sets ? `${exercise.sets} подх.` : "",
        exercise.reps ? `${exercise.reps} повт.` : "",
        exercise.suggested_weight_kg ? `${exercise.suggested_weight_kg} кг` : "",
      ].filter(Boolean).join(" · ");
      return `<li><span>${escapeHTML(exercise.name)}</span>${prescription ? `<small>${escapeHTML(prescription)}</small>` : ""}</li>`;
    }).join("");
    return `<article class="workout-card">
      <div class="workout-heading"><div><strong>${escapeHTML(workout.title)}</strong><span>${formatDate(when, { weekday: "short", day: "numeric", month: "short" })}, ${formatTime(when)}</span></div><b>${escapeHTML(labels[workout.status] || workout.status)}</b></div>
      ${workout.focus ? `<p>${escapeHTML(workout.focus)}</p>` : ""}
      ${exercises ? `<details><summary>Упражнения</summary><ul class="exercise-list">${exercises}</ul></details>` : ""}
      ${workout.rpe ? `<p class="workout-result">Нагрузка RPE ${escapeHTML(workout.rpe)}/10${workout.notes ? ` · ${escapeHTML(workout.notes)}` : ""}</p>` : ""}
    </article>`;
  }

  function renderHealth(data) {
    const snapshot = data.apple_health.snapshot;
    const recovery = data.recovery;
    const recommendations = (recovery.recommendations || []).map((item) => `<li>${escapeHTML(item)}</li>`).join("");
    const sleep = snapshot ? Number(snapshot.sleep_minutes || 0) : 0;
    const steps = snapshot ? Number(snapshot.steps || 0) : 0;
    const stepGoal = snapshot ? Number(snapshot.step_goal || 0) : 0;
    const workoutMinutes = snapshot ? Number(snapshot.workout_minutes || 0) : 0;
    const waterGoal = Number(data.goals.water_ml || 0);
    const proteinGoal = Number(data.goals.protein_g || 0);
    const water = Number(data.intake.water_ml || 0);
    const protein = Number(data.intake.protein_g || 0);
    const upcoming = data.training.upcoming || [];
    const history = data.training.history || [];
    const healthStatus = snapshot
      ? `${snapshot.fresh ? "Сегодня" : "Последние данные"} · ${formatDate(snapshot.captured_at, { day: "numeric", month: "short" })}, ${formatTime(snapshot.captured_at)}`
      : "Apple Health ещё не подключён";

    return `<section class="recovery-card ${escapeHTML(recovery.level)}"><span class="recovery-label">Баланс дня</span><h2>${escapeHTML(recovery.title)}</h2><p>${escapeHTML(recovery.message)}</p>${recommendations ? `<ul>${recommendations}</ul>` : ""}</section>
      <section class="section"><div class="section-heading"><h2>Сон и движение</h2><span>${escapeHTML(healthStatus)}</span></div>
        <div class="health-grid">
          ${healthMetricHTML("Сон", sleepValue(sleep), sleep ? "последняя ночь" : "нет данных")}
          ${healthMetricHTML("Шаги", steps ? steps.toLocaleString("ru-RU") : "—", stepGoal ? `цель ${stepGoal.toLocaleString("ru-RU")}` : "нет данных", stepGoal ? Math.round((steps / stepGoal) * 100) : null)}
          ${healthMetricHTML("Активность", workoutMinutes ? `${workoutMinutes} мин` : "—", snapshot?.active_energy_kcal ? `${snapshot.active_energy_kcal} ккал` : "нет данных")}
        </div>
        ${data.apple_health.bridge_enabled ? `<article class="integration-row standalone"><span class="integration-icon" aria-hidden="true">З</span><div><strong>Apple Health</strong><p>${snapshot ? "Данные поступают автоматически" : "Подключение выполняется один раз"}</p></div><button class="secondary-button compact" type="button" data-connection="health">${snapshot ? "Данные" : "Подключить"}</button></article>` : '<div class="notice"><strong>Apple Health выключен</strong><span>Остальные разделы продолжают работать.</span></div>'}
      </section>
      <section class="section"><div class="section-heading"><h2>Быстрый учёт</h2><span>сегодня</span></div>
        <div class="intake-grid">
          <article class="intake-card"><span>Вода</span><strong>${water} мл</strong><small>${waterGoal ? `цель ${waterGoal} мл` : "цель не задана"}</small><span class="health-progress"><i style="width:${waterGoal ? Math.min(100, Math.round((water / waterGoal) * 100)) : 0}%"></i></span><div class="counter-actions"><button type="button" data-intake-field="water_ml" data-intake-delta="-250">−250</button><button type="button" data-intake-field="water_ml" data-intake-delta="250">+250</button></div></article>
          <article class="intake-card"><span>Белок</span><strong>${protein} г</strong><small>${proteinGoal ? `цель ${proteinGoal} г` : "цель не задана"}</small><span class="health-progress"><i style="width:${proteinGoal ? Math.min(100, Math.round((protein / proteinGoal) * 100)) : 0}%"></i></span><div class="counter-actions"><button type="button" data-intake-field="protein_g" data-intake-delta="-20">−20</button><button type="button" data-intake-field="protein_g" data-intake-delta="20">+20</button></div></article>
        </div>
        <div class="nutrition-summary"><span>КБЖУ</span><strong>${Number(data.intake.calories_kcal || 0)} ккал</strong><small>Б ${protein} · Ж ${Number(data.intake.fat_g || 0)} · У ${Number(data.intake.carbs_g || 0)} г</small></div>
        <details class="health-goals"><summary>Изменить цели</summary><form id="health-goals-form"><div class="form-row"><label class="field"><span>Вода, мл</span><input name="water_ml" type="number" min="0" max="10000" step="250" value="${waterGoal}"></label><label class="field"><span>Белок, г</span><input name="protein_g" type="number" min="0" max="400" step="5" value="${proteinGoal}"></label></div><button class="primary-button full-width" type="submit">Сохранить цели</button></form></details>
      </section>
      <section class="section"><div class="section-heading"><h2>Тренировки</h2><span>${upcoming.length} впереди</span></div>
        <article class="micro-plan ${escapeHTML(data.training.micro_plan.level)}"><span>Следующий шаг</span><strong>${escapeHTML(data.training.micro_plan.title)}</strong><p>${escapeHTML(data.training.micro_plan.text)}</p></article>
        ${upcoming.length ? `<div class="workout-list">${upcoming.map((item) => workoutHTML(item)).join("")}</div>` : '<div class="empty-state"><strong>Тренировок пока нет</strong><span>Попроси бота составить план обычной фразой.</span></div>'}
        ${history.length ? `<details class="training-history"><summary>История тренировок</summary><div class="workout-list">${history.map((item) => workoutHTML(item, true)).join("")}</div></details>` : ""}
      </section><p class="medical-disclaimer">${escapeHTML(data.medical_disclaimer)}</p>`;
  }

  function renderHelp() {
    return `<section class="help-intro"><button class="back-link" type="button" data-back>← Назад</button><p>Основные действия выполняются обычными фразами в чате. Здесь — только одноразовая настройка и ответы на редкие вопросы.</p></section>
      <section class="faq-list">
        ${FAQ_ITEMS.map((item) => `<details ${item.open ? "open" : ""}><summary>${escapeHTML(item.question)}</summary>${item.answer}</details>`).join("")}
      </section>`;
  }

  function connectionSetupHTML(kind, config) {
    const health = kind === "health";
    const endpointId = health ? "health-shortcut-endpoint" : "shortcut-endpoint";
    const tokenId = health ? "health-shortcut-token" : "shortcut-token";
    return `<div class="sheet-handle" aria-hidden="true"></div><div class="sheet-heading"><div><p class="eyebrow">Одноразовая настройка</p><h2>${health ? "Apple Health" : "Календарь HSE"}</h2></div><button class="icon-button" type="button" data-close-setup aria-label="Закрыть">×</button></div>
      <p class="setup-summary">Скопируй эти два значения в свою команду. После этого синхронизация работает в фоне.</p>
      <label class="field compact-field"><span>URL приёмника</span><div class="copy-row"><input id="${endpointId}" readonly value="${escapeHTML(config.endpoint)}"><button class="secondary-button" type="button" data-copy-input="${endpointId}">Копировать</button></div></label>
      <label class="field compact-field"><span>Значение поля token</span><div class="copy-row"><input id="${tokenId}" type="password" readonly value="${escapeHTML(config.token)}"><button class="secondary-button" type="button" data-copy-input="${tokenId}">Копировать</button></div></label>
      <p class="secret-warning">Не отправляй token в чат и не добавляй его в ссылку.</p>
      <button class="support-link boxed" type="button" data-open-help><span>Открыть пошаговую инструкцию</span><b aria-hidden="true">›</b></button>`;
  }

  function resourceStatusHTML(entry) {
    if (entry.error && entry.data === null) {
      const message = entry.error.status === 401
        ? "Открой Mini App кнопкой «Управление» в чате с ботом."
        : entry.error.message;
      return `<section class="status-screen section-status" aria-live="polite"><span class="status-symbol" aria-hidden="true">!</span><h2>Раздел временно недоступен</h2><p>${escapeHTML(message)}</p><button class="primary-button" data-retry-current type="button">Повторить</button></section>`;
    }
    return `<section class="status-screen section-status" aria-live="polite"><span class="loader" aria-hidden="true"></span><p>Собираю актуальные данные…</p></section>`;
  }

  return {
    connectionSetupHTML,
    periodControlHTML,
    renderHealth,
    renderHelp,
    renderPlanner,
    renderProfile,
    renderTasks,
    renderToday,
    resourceStatusHTML,
  };
}
