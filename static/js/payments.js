/* ═══════════════════════════════════════════════════════════════
   Payments — PhonePe / Google Pay style flow

     1. Bottom sheet: Wallet ID + amount (quick-amount chips)
     2. Confirm: payee name is looked up and shown before money moves
     3. PIN: 4-digit payment PIN on a keypad (created on first payment)
     4. Full-screen "processing" ring while the real request runs
     5. Result: success burst (tick draws, ripples, confetti, amount
        counts up) or a failure screen - from the SERVER's answer,
        never faked.
   Depends on main.js (csrfToken, copyToClipboard, showToast).
   ═══════════════════════════════════════════════════════════════ */
const WalletPay = (function () {
    "use strict";
    const MIN_PROCESSING_MS = 1500; // so the animation reads even on a fast network
    const PIN_LEN = 4;
    let cfg = null, els = {}, state = { to: null, name: null, amount: 0, result: null };
    let pins = {};          // keypad controllers by name
    let createMode = "pay"; // after creating a PIN: continue paying, or just save

    const rupee = (n) => "\u20b9" + Number(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const $ = (id) => document.getElementById(id);
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const initial = (name) => (name || "?").trim().charAt(0).toUpperCase() || "?";
    const post = (url, params) => fetch(url, {
        method: "POST", credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrfToken(), Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams(Object.assign({ csrf_token: csrfToken() }, params)),
    });

    function init(options) {
        cfg = Object.assign({ transferUrl: "", recipientUrl: "", pinSetUrl: "", myId: "", hasPin: false, onSuccess: null }, options);
        els = {
            overlay: $("payOverlay"), stage: $("payStage"),
            to: $("payTo"), amount: $("payAmount"), err1: $("payErr1"), next: $("payNext"),
            payeeName: $("payPayeeName"), payeeId: $("payPayeeId"), payeeAv: $("payPayeeAv"),
            confirmAmt: $("payConfirmAmt"), confirmBtn: $("payConfirm"), back: $("payBack"),
        };
        if (!els.overlay || !els.stage) return;

        document.querySelectorAll("[data-open-pay]").forEach((b) => b.addEventListener("click", () => open(b.getAttribute("data-pay-to") || "")));
        document.querySelectorAll("[data-close-pay]").forEach((b) => b.addEventListener("click", close));
        document.querySelectorAll("[data-open-pin-settings]").forEach((b) => b.addEventListener("click", openPinSettings));
        els.overlay.addEventListener("click", (e) => { if (e.target === els.overlay) close(); });

        document.querySelectorAll(".pay-chip").forEach((c) => c.addEventListener("click", () => {
            els.amount.value = c.getAttribute("data-amt");
            els.amount.focus();
        }));
        els.to.addEventListener("input", () => { els.to.value = els.to.value.replace(/[^A-Za-z0-9]/g, "").slice(0, 16); });
        els.next.addEventListener("click", goConfirm);
        els.back.addEventListener("click", () => showStep(1));
        els.confirmBtn.addEventListener("click", afterConfirm);
        $("payPinBack").addEventListener("click", () => showStep(2));
        $("payForgotPin").addEventListener("click", (e) => { e.preventDefault(); openPinSettings(true); });
        $("payPinSave").addEventListener("click", savePinForm);
        [els.to, els.amount].forEach((i) => i.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); goConfirm(); } }));
        $("payDone").addEventListener("click", finish);
        $("payRetry").addEventListener("click", () => { hideStage(); open(state.to || ""); showStep(2); });
        $("payCopyTxn").addEventListener("click", () => copyToClipboard($("payTxn").textContent));

        pins.enter = pinPad("pinPadEnter", "pinDotsEnter", onEnterPin);
        pins.create = pinPad("pinPadCreate", "pinDotsCreate", onCreatePin);

        // physical keyboard works on the PIN steps too
        document.addEventListener("keydown", (e) => {
            if (!els.overlay.classList.contains("open")) return;
            if (e.key === "Escape") { close(); return; }
            const active = !$("payStep3").hidden ? pins.enter : !$("payStep4").hidden ? pins.create : null;
            if (!active) return;
            if (/^\d$/.test(e.key)) active.push(e.key);
            else if (e.key === "Backspace") active.pop();
        });
    }

    // ── PIN keypad (dots + 0-9 grid) ─────────────────────────
    function pinPad(padId, dotsId, onComplete) {
        const pad = $(padId), dots = $(dotsId);
        let value = "", locked = false;
        const keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "", "0", "\u232b"];
        pad.innerHTML = keys.map((k) => k === "" ? "<span></span>"
            : `<button type="button" class="pin-key${k === "\u232b" ? " pin-key-del" : ""}" data-k="${k}" aria-label="${k === "\u232b" ? "Delete" : k}">${k}</button>`).join("");
        dots.innerHTML = "<span></span>".repeat(PIN_LEN);
        const draw = () => [...dots.children].forEach((d, i) => d.classList.toggle("on", i < value.length));
        const api = {
            push(d) { if (locked || value.length >= PIN_LEN) return; value += d; draw(); if (value.length === PIN_LEN) { locked = true; setTimeout(() => onComplete(value), 140); } },
            pop() { if (locked) return; value = value.slice(0, -1); draw(); },
            clear() { value = ""; locked = false; draw(); },
            shake() { dots.classList.remove("shake"); void dots.offsetWidth; dots.classList.add("shake"); if (navigator.vibrate) navigator.vibrate(80); },
            disable(v) { pad.classList.toggle("disabled", !!v); locked = !!v; },
        };
        pad.addEventListener("click", (e) => {
            const b = e.target.closest(".pin-key");
            if (!b) return;
            b.getAttribute("data-k") === "\u232b" ? api.pop() : api.push(b.getAttribute("data-k"));
        });
        return api;
    }

    // ── sheet navigation ─────────────────────────────────────
    function showStep(n) {
        for (let i = 1; i <= 5; i++) { const el = $("payStep" + i); if (el) el.hidden = i !== n; }
        if (n === 1) setTimeout(() => (els.to.value ? els.amount : els.to).focus(), 60);
        if (n === 3) { pins.enter.clear(); pins.enter.disable(false); $("payErr3").textContent = ""; }
        if (n === 5) $("payPinErr").textContent = "";
    }
    function open(prefillId) {
        els.overlay.classList.add("open");
        document.body.classList.add("pay-open");
        els.err1.textContent = "";
        if (prefillId) els.to.value = prefillId;
        showStep(1);
    }
    function close() {
        els.overlay.classList.remove("open");
        document.body.classList.remove("pay-open");
    }
    function reopenAtStep(n) {
        els.overlay.classList.add("open");
        document.body.classList.add("pay-open");
        showStep(n);
    }

    async function goConfirm() {
        els.err1.textContent = "";
        const to = els.to.value.trim();
        const amount = parseFloat(els.amount.value);
        if (!/^[A-Za-z0-9]{16}$/.test(to)) { els.err1.textContent = "Enter the recipient's 16-character Wallet ID."; return; }
        if (to === cfg.myId) { els.err1.textContent = "You can't send money to yourself."; return; }
        if (!(amount >= 1)) { els.err1.textContent = "Enter an amount of at least \u20b91."; return; }

        els.next.disabled = true;
        const label = els.next.innerHTML;
        els.next.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
        try {
            const r = await fetch(`${cfg.recipientUrl}?id=${encodeURIComponent(to)}`, { headers: { "X-Requested-With": "XMLHttpRequest", Accept: "application/json" } });
            const d = await r.json().catch(() => ({}));
            if (r.status === 429) { els.err1.textContent = "Too many lookups. Please wait a moment."; return; }
            if (!d.found) { els.err1.textContent = d.error === "self" ? "You can't send money to yourself." : "No account found with that Wallet ID."; return; }
            state = { to: d.id, name: d.name, amount: Math.round(amount * 100) / 100, result: null };
            els.payeeName.textContent = d.name;
            els.payeeId.textContent = d.id;
            els.payeeAv.textContent = initial(d.name);
            els.confirmAmt.textContent = rupee(state.amount);
            els.confirmBtn.innerHTML = `<span style="position:relative;z-index:1">Continue to PIN</span>`;
            showStep(2);
        } catch (e) {
            els.err1.textContent = "Network problem. Check your connection and try again.";
        } finally {
            els.next.disabled = false;
            els.next.innerHTML = label;
        }
    }

    // ── PIN: enter / create / change ─────────────────────────
    function afterConfirm() {
        if (cfg.hasPin) {
            $("payPinFor").textContent = `Paying ${rupee(state.amount)} to ${state.name}`;
            showStep(3);
        } else {
            startCreate("pay");
        }
    }
    function onEnterPin(pin) { pay(pin); }

    let createFirst = null;
    function startCreate(mode) {
        createMode = mode;
        createFirst = null;
        $("payPinTitle").textContent = "Create payment PIN";
        $("payPinSub").textContent = mode === "pay"
            ? "Set a 4-digit PIN. You'll use it to approve every payment."
            : "Choose a 4-digit PIN to approve your payments.";
        $("payErr4").textContent = "";
        reopenAtStep(4);
        pins.create.clear(); pins.create.disable(false);
    }
    async function onCreatePin(pin) {
        const err = $("payErr4");
        if (createFirst === null) {
            createFirst = pin;
            $("payPinTitle").textContent = "Confirm your PIN";
            $("payPinSub").textContent = "Enter the same 4 digits again.";
            err.textContent = "";
            pins.create.clear();
            return;
        }
        if (pin !== createFirst) {
            err.textContent = "The PINs didn't match. Start again.";
            pins.create.shake(); createFirst = null;
            $("payPinTitle").textContent = "Create payment PIN";
            $("payPinSub").textContent = "Set a 4-digit PIN.";
            setTimeout(() => pins.create.clear(), 350);
            return;
        }
        pins.create.disable(true);
        try {
            const r = await post(cfg.pinSetUrl, { new_pin: pin, confirm_pin: pin });
            const d = await r.json().catch(() => ({}));
            if (!r.ok || !d.ok) {
                err.textContent = d.error || "Could not save your PIN. Please try again.";
                pins.create.shake(); createFirst = null;
                $("payPinTitle").textContent = "Create payment PIN";
                $("payPinSub").textContent = "Choose a different 4-digit PIN.";
                setTimeout(() => { pins.create.clear(); pins.create.disable(false); }, 350);
                return;
            }
            setHasPin(true);
            if (createMode === "pay") { pay(pin); }
            else { close(); showToast("Payment PIN saved.", "success"); }
        } catch (e) {
            err.textContent = "Network problem. Please try again.";
            createFirst = null; pins.create.clear(); pins.create.disable(false);
        }
    }

    function setHasPin(v) {
        cfg.hasPin = !!v;
        document.querySelectorAll("[data-pin-label]").forEach((el) => { el.textContent = cfg.hasPin ? "Change PIN" : "Set PIN"; });
    }

    // Change / reset PIN form (also reached from "Forgot PIN?")
    function openPinSettings(fromForgot) {
        if (!cfg.hasPin) { startCreate("setup"); return; }
        $("pinCurrent").value = ""; $("pinNew").value = ""; $("pinNew2").value = "";
        $("payPinErr").textContent = "";
        $("pinCurrentHint").textContent = fromForgot === true
            ? "Forgot your PIN? Enter your account password here."
            : "Current PIN \u2014 or your account password if you forgot it.";
        reopenAtStep(5);
        setTimeout(() => $("pinCurrent").focus(), 80);
    }
    async function savePinForm() {
        const err = $("payPinErr");
        const cur = $("pinCurrent").value, n1 = $("pinNew").value.trim(), n2 = $("pinNew2").value.trim();
        err.textContent = "";
        if (!cur) { err.textContent = "Enter your current PIN or account password."; return; }
        if (!/^\d{4}$/.test(n1)) { err.textContent = "New PIN must be exactly 4 digits."; return; }
        if (n1 !== n2) { err.textContent = "The two new PINs don't match."; return; }
        const btn = $("payPinSave"); btn.disabled = true;
        try {
            const r = await post(cfg.pinSetUrl, { current: cur, new_pin: n1, confirm_pin: n2 });
            const d = await r.json().catch(() => ({}));
            if (!r.ok || !d.ok) { err.textContent = d.error || "Could not save your PIN."; return; }
            close(); showToast("Payment PIN updated.", "success");
        } catch (e) {
            err.textContent = "Network problem. Please try again.";
        } finally { btn.disabled = false; }
    }

    // ── stage helpers ────────────────────────────────────────
    function resetStage() {
        els.stage.classList.remove("is-success", "is-fail");
        $("payStageTitle").textContent = "Processing payment";
        $("payStageAmount").textContent = rupee(state.amount);
        $("payStageSub").textContent = `Paying ${state.name}\u2026 please don't close this screen`;
        $("payConfetti").innerHTML = "";
    }
    function showStage() { els.stage.classList.add("open"); }
    function hideStage() { els.stage.classList.remove("open", "is-success", "is-fail"); }

    function confetti() {
        const box = $("payConfetti");
        const colors = ["#25d366", "#53bdeb", "#f5a623", "#ffffff", "#00a884", "#ff6a9c"];
        let html = "";
        for (let i = 0; i < 26; i++) {
            const ang = (Math.PI * 2 * i) / 26 + Math.random() * 0.4;
            const dist = 110 + Math.random() * 150;
            html += `<i style="--c:${colors[i % colors.length]};--dx:${(Math.cos(ang) * dist).toFixed(0)}px;--dy:${(Math.sin(ang) * dist - 40).toFixed(0)}px;--rot:${(Math.random() * 540 - 270).toFixed(0)}deg;animation-delay:${(0.42 + Math.random() * 0.18).toFixed(2)}s"></i>`;
        }
        box.innerHTML = html;
    }
    function countUp(el, to, ms) {
        const start = performance.now();
        const step = (t) => {
            const p = Math.min(1, (t - start) / ms);
            el.textContent = rupee(to * (1 - Math.pow(1 - p, 3)));
            if (p < 1) requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
    }

    async function pay(pin) {
        close();
        resetStage();
        showStage();
        const started = performance.now();

        let result;
        try {
            const r = await post(cfg.transferUrl, { to_referral_id: state.to, amount: String(state.amount), pin: pin });
            const d = await r.json().catch(() => ({ ok: false, error: "Unexpected response from the server." }));
            result = r.ok && d.ok ? d : { ok: false, code: d.code, error: d.error || "Payment failed. Please try again." };
        } catch (e) {
            result = { ok: false, error: "Network problem - your money was not sent. Please try again." };
        }

        // PIN problems go straight back to the keypad (no failure screen)
        if (!result.ok && (result.code === "pin_invalid" || result.code === "pin_locked")) {
            hideStage();
            reopenAtStep(3);
            $("payPinFor").textContent = `Paying ${rupee(state.amount)} to ${state.name}`;
            $("payErr3").textContent = result.error;
            pins.enter.shake();
            if (result.code === "pin_locked") pins.enter.disable(true);
            else setTimeout(() => pins.enter.clear(), 350);
            return;
        }
        if (!result.ok && result.code === "pin_not_set") { hideStage(); setHasPin(false); startCreate("pay"); return; }

        const wait = MIN_PROCESSING_MS - (performance.now() - started);
        if (wait > 0) await sleep(wait);
        result.ok ? success(result) : failure(result.error);
    }

    function success(d) {
        confetti();
        els.stage.classList.add("is-success");
        $("payStageTitle").textContent = "Payment successful";
        $("payStageSub").textContent = `Paid to ${d.to_name}`;
        $("payBadgeIcon").innerHTML = '<path class="stroke" d="M18 36l12 12 24-26"/>';
        countUp($("payStageAmount"), d.amount, 900);
        $("payTxn").textContent = d.txn_id;
        $("payWhen").textContent = d.created_at_display || "";
        $("payTo2").textContent = d.to_name;
        $("payBal").textContent = rupee(d.new_balance);
        if (navigator.vibrate) navigator.vibrate([18, 40, 28]);
        state.result = d;
    }
    function failure(msg) {
        els.stage.classList.add("is-fail");
        $("payStageTitle").textContent = "Payment failed";
        $("payStageAmount").textContent = rupee(state.amount);
        $("payStageSub").textContent = msg;
        $("payBadgeIcon").innerHTML = '<path class="stroke" d="M22 22l28 28"/><path class="stroke b" d="M50 22L22 50"/>';
        if (navigator.vibrate) navigator.vibrate([60, 40, 60]);
        state.result = null;
    }
    function finish() {
        hideStage();
        const d = state.result;
        if (d && typeof cfg.onSuccess === "function") cfg.onSuccess(d);
        state.result = null;
        els.to.value = ""; els.amount.value = "";
    }

    return { init, open, close, openPinSettings };
})();
