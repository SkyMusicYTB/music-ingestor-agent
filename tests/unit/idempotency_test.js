Object.defineProperty(globalThis, "document", {
  value: {
    cookie: "",
    querySelector: () => null,
    querySelectorAll: () => [],
    body: {dataset: {}},
  },
  configurable: true,
});

const {createIdempotencyKey} = await import("../../app/static/app.js");
const SAFE = /^[A-Za-z0-9._:-]{8,128}$/;

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

Deno.test("uses native randomUUID when available", () => {
  const expected = "11111111-1111-4111-8111-111111111111";
  const actual = createIdempotencyKey({randomUUID: () => expected});
  assert(actual === expected, `expected native UUID, got ${actual}`);
});

Deno.test("falls back to getRandomValues with UUIDv4 bits", () => {
  const actual = createIdempotencyKey({
    randomUUID: () => {
      throw new Error("Safari insecure-context behavior");
    },
    getRandomValues: (bytes) => {
      for (let index = 0; index < bytes.length; index += 1) bytes[index] = index;
      return bytes;
    },
  });
  assert(SAFE.test(actual), `unsafe key: ${actual}`);
  assert(actual[14] === "4", `not UUIDv4: ${actual}`);
  assert(/[89ab]/.test(actual[19]), `invalid UUID variant: ${actual}`);
});

Deno.test("last-resort fallback remains safe and unique", () => {
  const first = createIdempotencyKey({});
  const second = createIdempotencyKey(null);
  assert(SAFE.test(first), `unsafe fallback: ${first}`);
  assert(SAFE.test(second), `unsafe fallback: ${second}`);
  assert(first !== second, "fallback keys must be unique within one page lifetime");
});
