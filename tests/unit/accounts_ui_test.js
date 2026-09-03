import {buildAccountRequest} from "../../app/static/accounts.js";

const id = "01991234-5678-7123-9123-abcdef123456";

Deno.test("account requests keep credentials out of URLs and default generated passwords", () => {
  const action = buildAccountRequest("create-user", {username: "alice", must_change_password: true});
  if (action.url !== "/api/v1/admin/users" || action.body.temporary_password !== null || action.body.role !== "user" || action.confirm !== null) throw new Error("unexpected create contract");
  const reset = buildAccountRequest("reset-password", {temporary_password: "temporary-test-password", must_change_password: true}, {userId: id});
  if (reset.url.includes("temporary-test-password") || reset.body.temporary_password !== "temporary-test-password" || !reset.confirm) throw new Error("unsafe reset contract");
});

Deno.test("account mutation methods and scoped routes are fixed", () => {
  const action = buildAccountRequest("set-active", {}, {userId: id, value: "false"});
  if (action.method !== "PATCH" || action.body.is_active !== false || !action.confirm.includes("already-approved work continues")) throw new Error("incorrect deactivation contract");
  const own = buildAccountRequest("revoke-other-sessions", {});
  if (own.url !== "/api/v1/account/revoke-other-sessions" || !own.confirm) throw new Error("incorrect own session contract");
});

Deno.test("account action target IDs cannot inject a different URL", () => {
  let rejected = false;
  try {buildAccountRequest("reset-password", {}, {userId: "../../other"});} catch {rejected = true;}
  if (!rejected) throw new Error("unsafe target accepted");
});
