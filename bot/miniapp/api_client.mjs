export class ApiClient {
  constructor({ telegram, location, timeoutMs = 10_000 } = {}) {
    this.telegram = telegram;
    this.location = location || { search: "" };
    this.timeoutMs = timeoutMs;
  }

  headers(hasJsonBody = false) {
    const headers = {};
    if (this.telegram?.initData) {
      headers.Authorization = `tma ${this.telegram.initData}`;
    }
    const debugUser = new URLSearchParams(this.location.search || "").get("debug_user");
    if (debugUser) headers["X-Debug-User"] = debugUser;
    if (hasJsonBody) headers["Content-Type"] = "application/json";
    return headers;
  }

  async request(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    const isBlob = typeof Blob !== "undefined" && options.body instanceof Blob;
    const hasJsonBody = Boolean(options.body) && !isBlob;
    try {
      const response = await fetch(path, {
        ...options,
        signal: controller.signal,
        headers: { ...this.headers(hasJsonBody), ...(options.headers || {}) },
      });
      let payload = {};
      try {
        payload = await response.json();
      } catch (_error) {
        // Some successful endpoints intentionally have no response body.
      }
      if (!response.ok) {
        const error = new Error(payload.error || `Ошибка ${response.status}`);
        error.status = response.status;
        throw error;
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError") {
        const timeoutError = new Error("Сервис не ответил за 10 секунд. Попробуй ещё раз.");
        timeoutError.status = 408;
        throw timeoutError;
      }
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }
}

export function initializeTelegram(telegram, onThemeChange = () => {}) {
  if (!telegram) return;

  const applyTheme = () => {
    const bg = telegram.themeParams?.secondary_bg_color || "#f2f3f5";
    telegram.setHeaderColor?.(bg);
    telegram.setBackgroundColor?.(bg);
    telegram.setBottomBarColor?.(telegram.themeParams?.bg_color || "#ffffff");
    onThemeChange(telegram.themeParams || {});
  };

  telegram.ready();
  telegram.expand();
  telegram.enableClosingConfirmation?.();
  applyTheme();
  telegram.onEvent?.("themeChanged", applyTheme);
}
