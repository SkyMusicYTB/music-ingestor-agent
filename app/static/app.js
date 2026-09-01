"use strict";

function csrfToken() {
  const entry = document.cookie.split("; ").find((row) => row.startsWith("music_agent_csrf="));
  return entry ? decodeURIComponent(entry.split("=").slice(1).join("=")) : "";
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-CSRF-Token", csrfToken());
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, {...options, headers, credentials: "same-origin"});
  const payload = await response.json().catch(() => ({detail: "Unexpected server response"}));
  if (!response.ok) throw new Error(payload.detail || "Request failed");
  return payload;
}

const discover = document.querySelector("#discover-form");
if (discover) {
  discover.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submitter = event.submitter;
    const error = document.querySelector("#discover-error");
    error.textContent = "";
    try {
      const payload = await api("/api/v1/requests", {
        method: "POST",
        headers: {"Idempotency-Key": crypto.randomUUID()},
        body: JSON.stringify({text: discover.elements.text.value, action: submitter.value})
      });
      window.location.assign(payload.url);
    } catch (failure) { error.textContent = failure.message; }
  });
}

const approval = document.querySelector("#approval-form");
if (approval) {
  approval.addEventListener("submit", async (event) => {
    event.preventDefault();
    const ids = [...approval.querySelectorAll("input[name=track_ids]:checked")].map((item) => item.value);
    const error = document.querySelector("#approval-error");
    error.textContent = "";
    try {
      await api(`/api/v1/requests/${approval.dataset.requestId}/approval`, {
        method: "POST",
        body: JSON.stringify({track_ids: ids, acknowledge_rights: approval.elements.acknowledge_rights.checked})
      });
      window.location.assign("/downloads");
    } catch (failure) { error.textContent = failure.message; }
  });
}

const refine = document.querySelector("#refine-form");
if (refine) {
  refine.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const payload = await api(`/api/v1/requests/${refine.dataset.requestId}/refinements`, {
        method: "POST",
        headers: {"Idempotency-Key": crypto.randomUUID()},
        body: JSON.stringify({text: refine.elements.text.value})
      });
      window.location.assign(payload.url);
    } catch (failure) { window.alert(failure.message); }
  });
}

document.querySelectorAll(".job-action").forEach((button) => {
  button.addEventListener("click", async () => {
    const row = button.closest("[data-job-id]");
    try {
      await api(`/api/v1/jobs/${row.dataset.jobId}/${button.dataset.action}`, {method: "POST"});
      window.location.reload();
    } catch (failure) { window.alert(failure.message); }
  });
});

document.querySelectorAll(".review-form").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const selected = form.querySelector("input[name=option_id]:checked");
    const details = form.querySelector("details");
    const manual = details && details.open;
    const payload = {
      option_id: selected ? selected.value : null,
      artist: manual && form.elements.artist.value.trim() ? form.elements.artist.value.trim() : null,
      title: manual && form.elements.title.value.trim() ? form.elements.title.value.trim() : null,
      album: manual ? form.elements.album.value.trim() || null : null
    };
    const error = form.querySelector(".form-error");
    error.textContent = "";
    try {
      await api(`/api/v1/jobs/${form.dataset.jobId}/review`, {
        method: "POST",
        body: JSON.stringify(payload)
      });
      window.location.reload();
    } catch (failure) { error.textContent = failure.message; }
  });
});

document.querySelectorAll(".track-art").forEach((image) => {
  image.addEventListener("error", () => {
    if (image.dataset.fallbackSrc) {
      image.src = image.dataset.fallbackSrc;
      delete image.dataset.fallbackSrc;
    } else {
      image.closest(".track-art-shell").hidden = true;
    }
  });
});

const rescan = document.querySelector("#rescan-button");
if (rescan) {
  rescan.addEventListener("click", async () => {
    rescan.disabled = true;
    try { await api("/api/v1/library/rescan", {method: "POST"}); }
    catch (failure) { window.alert(failure.message); rescan.disabled = false; }
  });
}

if (document.cookie.includes("music_agent_session") || csrfToken()) {
  const live = document.querySelector("#live-status");
  const cursor = document.body.dataset.eventCursor;
  const eventUrl = /^\d+$/.test(cursor)
    ? `/api/v1/events?after=${encodeURIComponent(cursor)}`
    : "/api/v1/events";
  const stream = new EventSource(eventUrl);
  let reloadTimer;
  stream.addEventListener("update", (event) => {
    const data = JSON.parse(event.data);
    live.textContent = data.message;
    live.classList.add("visible");
    const page = document.body.dataset.livePage;
    if (page === "request" || page === "downloads" || page === "library") {
      clearTimeout(reloadTimer);
      reloadTimer = setTimeout(() => window.location.reload(), 1200);
    }
  });
  stream.addEventListener("reset", () => window.location.reload());
  stream.onerror = () => { live.textContent = "Reconnecting to live updates…"; live.classList.add("visible"); };
}
