"use strict";

const SAFE_IDEMPOTENCY_KEY = /^[A-Za-z0-9._:-]{8,128}$/;
let fallbackCounter = 0;

export function createIdempotencyKey(cryptoProvider = globalThis.crypto) {
  try {
    if (typeof cryptoProvider?.randomUUID === "function") {
      const generated = cryptoProvider.randomUUID();
      if (SAFE_IDEMPOTENCY_KEY.test(generated)) return generated;
    }
  } catch (_failure) {
    // Continue to the getRandomValues or non-crypto fallback.
  }

  try {
    if (typeof cryptoProvider?.getRandomValues === "function") {
      const bytes = new Uint8Array(16);
      cryptoProvider.getRandomValues(bytes);
      bytes[6] = (bytes[6] & 0x0f) | 0x40;
      bytes[8] = (bytes[8] & 0x3f) | 0x80;
      const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
      return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    }
  } catch (_failure) {
    // A broken/inaccessible Web Crypto implementation must not block a request.
  }

  fallbackCounter += 1;
  const entropy = Math.random().toString(36).slice(2) || "0";
  const fallback = `fallback-${Date.now().toString(36)}-${fallbackCounter.toString(36)}-${entropy}`;
  return fallback.slice(0, 128);
}

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
        headers: {"Idempotency-Key": createIdempotencyKey()},
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
        headers: {"Idempotency-Key": createIdempotencyKey()},
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
    const details = form.querySelector("details.review-correction");
    const manual = details && details.open;
    const correction = manual ? {
      artist: form.elements.artist.value.trim() || null,
      title: form.elements.title.value.trim() || null,
      album: form.elements.album.value.trim() || null
    } : null;
    const selections = [...form.querySelectorAll("fieldset[data-decision-id]")].map((fieldset, index) => {
      const selected = fieldset.querySelector("input[type=radio]:checked");
      return {
        decision_id: fieldset.dataset.decisionId,
        option_id: selected ? selected.value : null,
        correction: index === 0 ? correction : null
      };
    });
    const payload = {
      bundle_fingerprint: form.dataset.bundleFingerprint,
      revision: Number.parseInt(form.dataset.revision, 10),
      selections
    };
    const error = form.querySelector(".form-error");
    error.textContent = "";
    if (selections.some((selection) => !selection.option_id)) {
      error.textContent = "Choose one option for each exceptional decision.";
      return;
    }
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
