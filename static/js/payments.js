/* ═══════════════════════════════════════════════════════════════
   Payments — PhonePe / Google Pay style flow

     1. Bottom sheet: enter Wallet ID + amount (quick-amount chips)
     2. Same sheet, step 2: payee name is looked up and shown for
        confirmation before any money moves
     3. Full-screen "processing" ring while the real request runs
     4. Result: success burst (disc pops, tick draws, ripples, confetti,
        amount counts up) or a failure screen - based on the SERVER's
        answer, never faked.
   Depends on main.js (csrfToken, copyToClipboard, showToast).
   ═══════════════════════════════════════════════════════════════ */
const WalletPay = (function () {
    "use strict";
    const MIN_PROCESSING_MS = 1500; // so the animation reads even on a fast network
    let cfg = null, els = {}, state = { to: null, name: null, amount: 0 };

    const rupee = (n) => "\u20b9" + Number(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const $ = (id) => document.getElementById(id);
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const initial = (name) => (name || "?").trim().charAt(0).toUpperCase() || "?";

    function init(options) {
        cfg = Object.assign({ transferUrl: "", recipientUrl: "", myId: "", onSuccess: null }, options);
        els = {
            overlay: $("payOverlay"), stage: $("payStage"),
            step1: $("payStep1"), step2: $("payStep2"),
            to: $("payTo"), amount: $("payAmount"), err1: $("payErr1"), next: $("payNext"),
            payeeName: $("payPayeeName"), payeeId: $("payPayeeId"), payeeAv: $("payPayeeAv"),
            confirmAmt: $("payConfirmAmt"), confirmBtn: $("payConfirm"), back: $("payBack"), err2: $("payErr2"),
        };
        if (!els.overlay || !els.stage) return;

        document.querySelectorAll("[data-open-pay]").forEach((b) => b.addEventListener("click", () => open(b.getAttribute("data-pay-to") || "")));
        document.querySelectorAll("[data-close-pay]").forEach((b) => b.addEventListener("click", close));
        els.overlay.addEventListener("click", (e) => { if (e.target === els.overlay) close(); });
        document.addEventListener("keydown", (e) => { if (e.key === "Escape" && els.overlay.classList.contains("open")) close(); });

        document.querySelectorAll(".pay-chip").forEach((c) => c.addEventListener("click", () => {
            els.amount.value = c.getAttribute("data-amt");
            els.amount.focus();
        }));
        els.to.addEventListener("input", () => { els.to.value = els.to.value.replace(/[^A-Za-z0-9]/g, "").slice(0, 16); });
        els.next.addEventListener("click", goConfirm);
        els.back.addEventListener("click", () => showStep(1));
        els.confirmBtn.addEventListener("click", pay);
        [els.to, els.amount].forEach((i) => i.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); goConfirm(); } }));
        $("payDone").addEventListener("click", finish);
        $("payRetry").addEventListener("click", () => { hideStage(); open(state.to || ""); showStep(2); });
        $("payCopyTxn").addEventListener("click", () => copyToClipboard($("payTxn").textContent));
    }

    function showStep(n) {
        els.step1.hidden = n !== 1;
        els.step2.hidden = n !== 2;
        if (n === 1) setTimeout(() => (els.to.value ? els.amount : els.to).focus(), 60);
    }
    function open(prefillId) {
        els.overlay.classList.add("open");
        document.body.classList.add("pay-open");
        els.err1.textContent = ""; els.err2.textContent = "";
        if (prefillId) els.to.value = prefillId;
        showStep(1);
    }
    function close() {
        els.overlay.classList.remove("open");
        document.body.classList.remove("pay-open");
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
            state = { to: d.id, name: d.name, amount: Math.round(amount * 100) / 100 };
            els.payeeName.textContent = d.name;
            els.payeeId.textContent = d.id;
            els.payeeAv.textContent = initial(d.name);
            els.confirmAmt.textContent = rupee(state.amount);
            els.confirmBtn.innerHTML = `<span style="position:relative;z-index:1">Pay ${rupee(state.amount)}</span>`;
            showStep(2);
        } catch (e) {
            els.err1.textContent = "Network problem. Check your connection and try again.";
        } finally {
            els.next.disabled = false;
            els.next.innerHTML = label;
        }
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

    async function pay() {
        els.err2.textContent = "";
        close();
        resetStage();
        showStage();
        const started = performance.now();

        let result;
        try {
            const body = new URLSearchParams({ to_referral_id: state.to, amount: String(state.amount), csrf_token: csrfToken() });
            const r = await fetch(cfg.transferUrl, {
                method: "POST",
                headers: { "X-Requested-With": "XMLHttpRequest", "X-CSRFToken": csrfToken(), Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
                body, credentials: "same-origin",
            });
            const d = await r.json().catch(() => ({ ok: false, error: "Unexpected response from the server." }));
            result = r.ok && d.ok ? d : { ok: false, error: d.error || "Payment failed. Please try again." };
        } catch (e) {
            result = { ok: false, error: "Network problem - your money was not sent. Please try again." };
        }

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

    return { init, open, close };
})();
