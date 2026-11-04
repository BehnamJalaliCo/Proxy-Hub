import { describe, expect, it } from "vitest";

import { actionLabel, actorLabel, errorCodeText, reasonLabel } from "./labels";
import {
  apiHashProblem,
  apiIdProblem,
  canStartLogin,
  checkErrorText,
  codeViaText,
  disconnectReasonText,
  isPending,
  MTP_STATE,
  mtpStateInfo,
  normalizeCode,
  normalizePhone,
  passwordProblem,
} from "./mtproxy";
import type { MtpAccount, MtpState } from "./types";

const PERSIAN = /[آ-ی]/;
// Ofcom's range for drama (+44 7700 900xxx) is never assigned to anyone.
const PHONE = "+447700900123";

const account = (state: MtpState): MtpAccount => ({
  state,
  phone_masked: "+44•••••23",
  account: null,
  login: null,
  mtproxybot_ok: null,
  connected_at: null,
  last_check_at: null,
  last_check_error: null,
  disconnected_at: null,
  disconnect_reason: null,
  error: null,
  notice: null,
  kill_switch: true,
  check_interval_min: 30,
});

describe("operator account inputs (mirror panel/proxyhub/mtproxy/validation.py)", () => {
  it("accepts an api_id of digits, Persian digits too", () => {
    expect(apiIdProblem("1234567")).toBeNull();
    expect(apiIdProblem(" ۱۲۳۴۵۶۷ ")).toBeNull();
    for (const bad of ["", "0", "0123", "12a", "-5", "99999999999", String(2 ** 31)]) expect(apiIdProblem(bad), bad).toMatch(PERSIAN);
  });

  it("accepts an api_hash of exactly 32 hex characters and never echoes a bad one", () => {
    expect(apiHashProblem("0123456789abcdef0123456789ABCDEF")).toBeNull();
    for (const bad of ["", "0123456789abcdef", "g".repeat(32), "0123456789abcdef0123456789abcdef0"]) {
      const problem = apiHashProblem(bad);
      expect(problem).toMatch(PERSIAN);
      if (bad !== "") expect(problem).not.toContain(bad);
    }
  });

  it("normalises a phone to E.164 like the server", () => {
    for (const typed of ["+44 7700 900123", "+44 (7700) 900-123", "0044 7700 900123", "+۴۴۷۷۰۰۹۰۰۱۲۳", "‎+447700900123"]) {
      expect(normalizePhone(typed)).toEqual({ ok: true, value: PHONE });
    }
    for (const bad of ["", "447700900123", "07700900123", "+0447700900123", "+123456", "+1234567890123456"]) {
      const parsed = normalizePhone(bad);
      expect(parsed.ok, bad).toBe(false);
      if (!parsed.ok) {
        expect(parsed.error).toMatch(PERSIAN);
        if (bad !== "") expect(parsed.error).not.toContain(bad);
      }
    }
  });

  it("takes a 5 to 8 digit code and any non-empty password", () => {
    expect(normalizeCode(" 12 345 ")).toEqual({ ok: true, value: "12345" });
    expect(normalizeCode("۱۲۳۴۵")).toEqual({ ok: true, value: "12345" });
    for (const bad of ["", "1234", "123456789", "12a45"]) expect(normalizeCode(bad).ok).toBe(false);
    expect(passwordProblem(" spaces count ")).toBeNull();
    expect(passwordProblem("")).toMatch(PERSIAN);
    expect(passwordProblem("x".repeat(257))).toMatch(PERSIAN);
  });
});

describe("operator account states", () => {
  it("names every state in Persian, unknown ones as they are", () => {
    for (const state of Object.keys(MTP_STATE) as MtpState[]) expect(mtpStateInfo(state).label).toMatch(PERSIAN);
    expect(mtpStateInfo("connected").tone).toBe("success");
    expect(mtpStateInfo("disconnected").tone).toBe("danger");
    expect(mtpStateInfo("brand_new").label).toBe("brand_new");
  });

  it("shows the login form only without a live session", () => {
    expect(canStartLogin(account("none"))).toBe(true);
    expect(canStartLogin(account("disconnected"))).toBe(true);
    for (const state of ["code_sent", "password_needed", "connected", "error"] as MtpState[]) expect(canStartLogin(account(state))).toBe(false);
    expect(isPending(account("code_sent"))).toBe(true);
    expect(isPending(account("password_needed"))).toBe(true);
    expect(isPending(account("connected"))).toBe(false);
  });

  it("explains where the code went, why a check failed and why a session ended", () => {
    for (const via of ["app", "sms", "call", "email", "fragment", "sms_word", "other", null]) expect(codeViaText(via)).toMatch(PERSIAN);
    expect(codeViaText("app")).toContain("اپ تلگرام");
    expect(checkErrorText(null)).toBeNull();
    for (const code of ["telegram_unavailable", "flood_wait", "mtproxybot_not_found", "session_unreadable", "session_missing", "api_id_invalid", "x"]) {
      expect(checkErrorText(code)).toMatch(PERSIAN);
    }
    for (const reason of ["session_revoked", "auth_key_unregistered", "user_deactivated", "user_deactivated_ban", "session_expired", null]) {
      expect(disconnectReasonText(reason)).toMatch(PERSIAN);
    }
    expect(disconnectReasonText("session_revoked")).toContain("دستگاه‌ها");
  });
});

describe("operator account labels", () => {
  it("names every action the panel writes and its loop in Persian", () => {
    const actions = [
      "mtproxy.account_code_sent",
      "mtproxy.account_password_needed",
      "mtproxy.account_connected",
      "mtproxy.account_login_failed",
      "mtproxy.account_login_cancelled",
      "mtproxy.account_logged_out",
      "mtproxy.account_disconnected",
      "mtproxy.account_check_failed",
      "mtproxy.account_check_ok",
    ];
    for (const a of actions) expect(actionLabel(a), a).toMatch(PERSIAN);
    expect(actorLabel("mtproxy")).toMatch(PERSIAN);
  });

  it("names every reason of those actions in Persian", () => {
    const reasons = [
      "code_via:app",
      "code_via:sms",
      "mtproxybot_ok",
      "mtproxybot_not_found",
      "get_me_ok",
      "owner",
      "telegram_logout",
      "session_already_ended",
      "session_unreadable",
      "session_missing",
      "credentials_missing",
      "credentials_only",
      "auth_key_unregistered",
      "session_revoked",
      "session_expired",
      "user_deactivated",
      "user_deactivated_ban",
      "api_id_invalid",
      "phone_number_invalid",
      "phone_number_banned",
      "phone_number_unoccupied",
      "flood_wait",
      "phone_code_invalid",
      "phone_code_expired",
      "password_invalid",
      "too_many_attempts",
      "telegram_unavailable",
      "mtproxy_login_expired",
    ];
    for (const r of reasons) expect(reasonLabel(r), r).toMatch(PERSIAN);
    expect(reasonLabel("code_via:app")).toBe("کد از راه: اپ تلگرام");
  });

  it("has a Persian sentence for every error code of the API", () => {
    const codes = [
      "api_id_format",
      "api_hash_format",
      "phone_format",
      "code_format",
      "password_format",
      "api_id_invalid",
      "api_id_published",
      "phone_number_invalid",
      "phone_number_banned",
      "phone_number_unoccupied",
      "phone_number_flood",
      "flood_wait",
      "password_flood",
      "phone_code_invalid",
      "phone_code_expired",
      "password_invalid",
      "too_many_attempts",
      "send_code_unavailable",
      "send_code_payment_required",
      "update_app_to_login",
      "telegram_refused",
      "telegram_unavailable",
      "logout_unavailable",
      "mtproxy_connected",
      "mtproxy_not_connected",
      "mtproxy_no_login",
      "mtproxy_login_expired",
      "mtproxy_busy",
      "vault_unavailable",
      "state_unavailable",
    ];
    for (const code of codes) expect(errorCodeText(code), code).toMatch(PERSIAN);
  });
});
