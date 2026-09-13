export function escapeHTML(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  })[character]);
}

export function parseDate(value) {
  return value ? new Date(value) : null;
}

export function plural(number, one, few, many) {
  const n10 = number % 10;
  const n100 = number % 100;
  if (n10 === 1 && n100 !== 11) return one;
  if (n10 >= 2 && n10 <= 4 && !(n100 >= 12 && n100 <= 14)) return few;
  return many;
}

export function sleepValue(minutes) {
  if (!minutes) return "—";
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} ч ${rest} мин` : `${hours} ч`;
}

export function createDateFormatters(getTimezone) {
  const optionsWithTimezone = (options = {}) => {
    const timezone = getTimezone();
    return timezone ? { ...options, timeZone: timezone } : options;
  };

  function formatTime(value) {
    const date = parseDate(value);
    return date
      ? new Intl.DateTimeFormat(
        "ru-RU",
        optionsWithTimezone({ hour: "2-digit", minute: "2-digit" }),
      ).format(date)
      : "";
  }

  function formatDate(value, options = {}) {
    const date = parseDate(value);
    return date
      ? new Intl.DateTimeFormat("ru-RU", optionsWithTimezone(options)).format(date)
      : "";
  }

  function dateKey(value) {
    const date = parseDate(value);
    if (!date) return "";
    const parts = new Intl.DateTimeFormat(
      "en",
      optionsWithTimezone({ year: "numeric", month: "2-digit", day: "2-digit" }),
    ).formatToParts(date);
    const byType = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${byType.year}-${byType.month}-${byType.day}`;
  }

  return { dateKey, formatDate, formatTime };
}
