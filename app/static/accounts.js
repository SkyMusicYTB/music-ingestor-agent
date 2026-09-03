// Account secrets live only in request/response memory and the one-time dialog.
// No localStorage, sessionStorage, query parameters or analytics are used.

export function buildAccountRequest(action, values, target = {}) {
  const id = target.userId;
  const user = target.username || "this account";
  const needsTarget = ["reset-password", "revoke-sessions", "set-active", "set-role", "set-password-required"].includes(action);
  if (needsTarget && !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(id || "")) {
    throw new Error("Invalid account identifier. Reload the page.");
  }
  const passwordBody = {
    temporary_password: values.temporary_password || null,
    must_change_password: values.must_change_password === true,
  };
  switch (action) {
    case "create-user":
      return {url: "/api/v1/admin/users", method: "POST", body: {username: values.username, role: values.role || "user", ...passwordBody}, confirm: values.role === "admin" ? "Create an administrator with full account-management access?" : null};
    case "reset-password":
      return {url: `/api/v1/admin/users/${id}/reset-password`, method: "POST", body: passwordBody, confirm: `Reset the password for ${user}? All their sessions will be signed out. History and music remain intact.`};
    case "set-active":
      return {url: `/api/v1/admin/users/${id}`, method: "PATCH", body: {is_active: target.value === "true"}, confirm: target.value === "true" ? `Activate ${user}? Previously revoked sessions remain signed out.` : `Deactivate ${user}? Access ends immediately. History and music remain intact, and already-approved work continues.`};
    case "set-role":
      return {url: `/api/v1/admin/users/${id}`, method: "PATCH", body: {role: target.value}, confirm: `Change ${user} to ${target.value === "admin" ? "Admin" : "User"}? Their sessions will be signed out.`};
    case "set-password-required":
      return {url: `/api/v1/admin/users/${id}`, method: "PATCH", body: {must_change_password: target.value === "true"}, confirm: `Change the password-change requirement for ${user}? Their sessions will be signed out.`};
    case "revoke-sessions":
      return {url: `/api/v1/admin/users/${id}/revoke-sessions`, method: "POST", body: {}, confirm: `Sign out all sessions for ${user}? Their password and history will not change.`};
    case "change-password":
      return {url: "/api/v1/account/change-password", method: "POST", body: {current_password: values.current_password || null, new_password: values.new_password, confirmation: values.confirmation}, confirm: null};
    case "revoke-other-sessions":
      return {url: "/api/v1/account/revoke-other-sessions", method: "POST", body: {}, confirm: "Sign out all your other sessions? This session stays signed in."};
    default:
      throw new Error("Unknown account action.");
  }
}

function csrfToken() {
  const entry = document.cookie.split("; ").find((row) => row.startsWith("music_agent_csrf="));
  return entry ? decodeURIComponent(entry.slice(entry.indexOf("=") + 1)) : "";
}

async function sendAccountRequest(action) {
  const response = await fetch(action.url, {
    method: action.method,
    credentials: "same-origin",
    cache: "no-store",
    headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken()},
    body: JSON.stringify(action.body),
  });
  const payload = await response.json();
  if (!response.ok) {
    const detail = payload.detail;
    const error = new Error(typeof detail === "string" ? detail : detail?.message || "The account action could not be completed. Check the form and try again.");
    error.code = typeof detail === "object" && !Array.isArray(detail) ? detail?.code : null;
    throw error;
  }
  return payload;
}

function showDialog(dialog) {
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
  dialog.querySelector("input,button")?.focus();
}

function closeDialog(dialog) {
  if (typeof dialog.close === "function") dialog.close();
  else dialog.removeAttribute("open");
}

function confirmAction(message) {
  const dialog = document.getElementById("account-confirmation");
  dialog.querySelector("[data-confirm-message]").textContent = message;
  showDialog(dialog);
  return new Promise((resolve) => {
    const yes = dialog.querySelector("[data-confirm]");
    const no = dialog.querySelector("[data-cancel]");
    const finish = (answer) => {
      yes.removeEventListener("click", accept);
      no.removeEventListener("click", reject);
      dialog.removeEventListener("cancel", cancel);
      closeDialog(dialog);
      resolve(answer);
    };
    const accept = () => finish(true);
    const reject = () => finish(false);
    const cancel = (event) => {event.preventDefault(); finish(false);};
    yes.addEventListener("click", accept);
    no.addEventListener("click", reject);
    dialog.addEventListener("cancel", cancel);
  });
}

function disableButtons(form, disabled) {
  form.querySelectorAll("button").forEach((button) => {button.disabled = disabled;});
}

function reauthenticate() {
  const dialog = document.getElementById("account-reauthentication");
  const form = dialog.querySelector("form");
  const password = form.elements.namedItem("current_password");
  const error = form.querySelector(".form-error");
  password.value = "";
  error.textContent = "";
  showDialog(dialog);
  return new Promise((resolve) => {
    let completed = false;
    const cancelButton = dialog.querySelector("[data-cancel]");
    const finish = (success) => {
      if (completed) return;
      completed = true;
      password.value = "";
      form.removeEventListener("submit", submit);
      cancelButton.removeEventListener("click", cancel);
      dialog.removeEventListener("cancel", cancel);
      closeDialog(dialog);
      resolve(success);
    };
    const cancel = (event) => {event.preventDefault(); finish(false);};
    const submit = async (event) => {
      event.preventDefault();
      disableButtons(form, true);
      try {
        await sendAccountRequest({url: "/api/v1/admin/reauthenticate", method: "POST", body: {current_password: password.value}});
        finish(true);
      } catch (failure) {
        password.value = "";
        error.textContent = failure.message;
      } finally {
        disableButtons(form, false);
      }
    };
    form.addEventListener("submit", submit);
    cancelButton.addEventListener("click", cancel);
    dialog.addEventListener("cancel", cancel);
  });
}

let unsavedPassword = false;

function clearSecretFields() {
  document.querySelectorAll('input[type="password"], [data-generated-password]').forEach((input) => {input.value = "";});
}

function showOneTimePassword(password) {
  const dialog = document.getElementById("account-generated-password");
  const input = dialog.querySelector("[data-generated-password]");
  const status = dialog.querySelector("[data-password-status]");
  const copy = dialog.querySelector("[data-copy-password]");
  const saved = dialog.querySelector("[data-password-saved]");
  input.value = password;
  status.textContent = "Save this now; it is not stored by the app.";
  unsavedPassword = true;
  document.body.dataset.accountSecretVisible = "true";
  showDialog(dialog);
  return new Promise((resolve) => {
    const preventCancel = (event) => event.preventDefault();
    const copyPassword = async () => {
      try {
        if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
        await navigator.clipboard.writeText(input.value);
        unsavedPassword = false;
        status.textContent = "Copied. Share it privately, then select I have saved it.";
      } catch {
        input.focus();
        input.select();
        status.textContent = "Automatic copying is unavailable here. Copy the selected password manually, then select I have saved it.";
      }
    };
    const acknowledge = () => {
      unsavedPassword = false;
      delete document.body.dataset.accountSecretVisible;
      input.value = "";
      copy.removeEventListener("click", copyPassword);
      saved.removeEventListener("click", acknowledge);
      dialog.removeEventListener("cancel", preventCancel);
      closeDialog(dialog);
      resolve();
    };
    copy.addEventListener("click", copyPassword);
    saved.addEventListener("click", acknowledge);
    dialog.addEventListener("cancel", preventCancel);
  });
}

if (typeof document !== "undefined") {
  let submitting = false;
  document.querySelectorAll("form[data-account-action]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (submitting) return;
      submitting = true;
      const error = form.querySelector(".form-error");
      error.textContent = "";
      disableButtons(form, true);
      try {
        const values = Object.fromEntries(new FormData(form));
        values.must_change_password = form.elements.namedItem("must_change_password")?.checked === true;
        const action = buildAccountRequest(form.dataset.accountAction, values, form.dataset);
        if (action.confirm && !await confirmAction(action.confirm)) return;
        let result;
        try {
          result = await sendAccountRequest(action);
        } catch (failure) {
          if (failure.code !== "reauthentication_required") throw failure;
          if (!await reauthenticate()) return;
          result = await sendAccountRequest(action);
        }
        clearSecretFields();
        if (typeof result.temporary_password === "string") {
          await showOneTimePassword(result.temporary_password);
          result.temporary_password = null;
        }
        if (result.redirect === "/") window.location.assign("/");
        else if (form.dataset.accountAction === "revoke-other-sessions") error.textContent = "All other sessions have been signed out.";
        else window.location.reload();
      } catch (failure) {
        error.textContent = failure.message;
      } finally {
        clearSecretFields();
        disableButtons(form, false);
        submitting = false;
      }
    });
  });
  window.addEventListener("beforeunload", (event) => {
    if (unsavedPassword) {event.preventDefault(); event.returnValue = "";}
  });
  window.addEventListener("pagehide", () => {clearSecretFields(); unsavedPassword = false;});
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {clearSecretFields(); window.location.reload();}
  });
}
