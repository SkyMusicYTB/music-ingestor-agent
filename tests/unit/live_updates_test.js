const listeners = new Map();
let currentContainer;
let replacements;
let status;

function container() {
  return {
    contains: () => false,
    replaceWith: (next) => { currentContainer = next; replacements += 1; },
  };
}

Object.defineProperty(globalThis, "document", {
  configurable: true,
  value: {
    cookie: "",
    body: {dataset: {livePage: "downloads"}},
    activeElement: null,
    querySelector: (selector) => selector === "#downloads-content" ? currentContainer : selector === "#downloads-status" ? status : null,
    querySelectorAll: () => [],
    getElementById: () => null,
    addEventListener: (name, callback) => {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(callback);
    },
  },
});
globalThis.window = {location: {href: "http://music-server/downloads?view=hidden&page=2"}};
globalThis.DOMParser = class {
  parseFromString() { return {querySelector: () => container()}; }
};
const {refreshFragment} = await import("../../app/static/app.js");

function assert(condition, message) {
  if (!condition) throw new Error(message);
}
function reset() {
  currentContainer = container();
  replacements = 0;
  status = {textContent: "", focus: () => {}};
}
const fragmentResponse = () => ({ok: true, redirected: false, text: async () => "fragment"});
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

Deno.test("an event during a fragment fetch triggers another fetch without losing filters", async () => {
  reset();
  let release;
  const firstResponse = new Promise((resolve) => { release = resolve; });
  const urls = [];
  globalThis.fetch = (url) => {
    urls.push(String(url));
    return urls.length === 1 ? firstResponse : Promise.resolve(fragmentResponse());
  };
  const first = refreshFragment();
  await refreshFragment();
  assert(urls.length === 1, "overlapping fetches must be serialized");
  release(fragmentResponse());
  await first;
  await settle();
  assert(urls.length === 2 && replacements === 2, "newer server state was lost behind the first response");
  assert(urls.every((url) => url.includes("view=hidden") && url.includes("page=2") && url.includes("fragment=true")), "live updates discarded navigation state");
});

function edit(jobId) {
  for (const listener of listeners.get("input")) {
    listener({target: {closest: () => ({dataset: {jobId}})}});
  }
}

async function cancel(jobId) {
  const error = {textContent: ""};
  const row = {dataset: {jobId}, querySelector: () => error};
  const button = {disabled: false, dataset: {action: "cancel"}, closest: () => row};
  await listeners.get("click")[0]({target: {closest: () => button}});
  assert(!button.disabled && !error.textContent, "cancellation did not complete");
}

Deno.test("cancelling an edited review unblocks updates without discarding other edits", async () => {
  reset();
  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(String(url));
    return String(url).startsWith("/api/") ? {ok: true, json: async () => ({})} : fragmentResponse();
  };
  edit("first-job");
  edit("second-job");
  await refreshFragment();
  assert(urls.length === 0, "review edits must defer fragment replacement");
  await cancel("first-job");
  assert(replacements === 0 && urls.length === 1, "cancelling one job discarded a different review");
  await cancel("second-job");
  await settle();
  assert(replacements === 1 && urls.length === 3, "cancelling the final edited review left updates blocked");
});
