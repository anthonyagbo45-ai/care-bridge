/* Open-page alerts only: no clinical data, background push or draft storage. */
(() => {
  "use strict";
  const panel = document.querySelector("[data-case-updates]");
  if (!panel) return;
  const message = panel.querySelector("[data-update-message]");
  const help = panel.querySelector("[data-update-help]");
  const refresh = panel.querySelector("[data-refresh-updates]");
  const baseline = panel.dataset.revision;
  let timer, controller, checking = false, stopped = false, suspended = false, dirty = false;
  let delay = 15000;

  document.querySelectorAll("form").forEach(form => {
    // A rejected POST already contains a draft even before the next keystroke.
    if ([...form.querySelectorAll("textarea")].some(field => field.value.length > 0)) dirty = true;
    form.addEventListener("input", () => { dirty = true; });
    form.addEventListener("change", () => { dirty = true; });
  });
  refresh.addEventListener("click", event => {
    if (dirty && !window.confirm("Refresh this page? Your unfinished entries will be lost.")) {
      event.preventDefault();
    }
  });

  function schedule() {
    clearTimeout(timer);
    if (!stopped && !suspended && !document.hidden) timer = setTimeout(check, delay);
  }

  async function check() {
    if (checking || stopped || suspended || document.hidden) return;
    checking = true;
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(panel.dataset.url, {
        credentials: "same-origin", cache: "no-store", redirect: "error",
        headers: { "Accept": "application/json" }, signal: controller.signal
      });
      if (response.status === 401 || response.status === 403) {
        stopped = true;
        panel.classList.add("updates-paused");
        message.textContent = "Your access has changed. Refresh to check your account.";
        help.textContent = "No further updates are being checked. Unfinished entries have stayed on this page.";
        return;
      }
      if (!response.ok || !response.headers.get("content-type")?.includes("application/json")) {
        throw new Error("Updates unavailable");
      }
      const data = await response.json();
      if (typeof data.revision !== "string" || !/^[0-9a-f]{64}$/.test(data.revision)) {
        throw new Error("Invalid update marker");
      }
      if (data.revision !== baseline) {
        stopped = true;
        panel.classList.add("updates-ready");
        message.textContent = "New updates are available.";
        help.textContent = "Refresh to see the latest cases, replies or report and call status. Your page has not been replaced.";
        refresh.textContent = "Show updates";
        return;
      }
      delay = 15000;
      panel.classList.remove("updates-paused");
      message.textContent = "Up to date. Checking while this page is visible.";
    } catch {
      if (!document.hidden && !suspended) {
        delay = Math.min(delay * 2, 120000);
        panel.classList.add("updates-paused");
        message.textContent = "Update checks are unavailable. Retrying shortly.";
      }
    } finally {
      clearTimeout(timeout);
      checking = false;
      schedule();
    }
  }

  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer);
    if (document.hidden) controller?.abort();
    else check();
  });
  window.addEventListener("pagehide", () => {
    suspended = true;
    clearTimeout(timer);
    controller?.abort();
  });
  window.addEventListener("pageshow", () => {
    suspended = false;
    schedule();
  });
  schedule();
})();
