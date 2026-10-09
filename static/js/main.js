// ─── Show/Hide Password + toast auto-hide ─────────────────
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.toggle-password').forEach(btn => {
        btn.addEventListener('click', function () {
            const input = this.parentElement.querySelector('.password-field');
            const icon = this.querySelector('i');
            if (!input) return;
            const show = input.type === 'password';
            input.type = show ? 'text' : 'password';
            if (icon) {
                icon.classList.toggle('bi-eye', !show);
                icon.classList.toggle('bi-eye-slash', show);
            }
        });
    });

    // Auto-hide flash toasts after 5s - except a fallback password-reset
    // link, which must never silently disappear before an admin can copy it
    document.querySelectorAll('.wa-toasts .toast').forEach(toast => {
        const text = (toast.querySelector('.toast-body') || {}).textContent || '';
        if (text.includes('Reset Link:')) return;
        setTimeout(() => {
            if (window.bootstrap) bootstrap.Toast.getOrCreateInstance(toast).hide();
            else toast.remove();
        }, 5000);
    });
});

// ─── CSRF token for fetch() calls ─────────────────────────
function csrfToken() {
    const m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.content : '';
}

// ─── Copy to Clipboard ────────────────────────────────────
// navigator.clipboard is undefined on non-HTTPS pages; calling .writeText on
// it threw a TypeError before the old .catch() fallback could ever run.
function copyToClipboard(text) {
    const done = () => showToast('Copied to clipboard!', 'success');
    const fallback = () => {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); done(); }
        catch (e) { showToast('Could not copy - please copy it manually.', 'danger'); }
        document.body.removeChild(ta);
    };
    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done).catch(fallback);
    } else {
        fallback();
    }
}

// ─── Toast Notification ───────────────────────────────────
// Uses textContent (not innerHTML) so a message can never inject markup.
function showToast(message, type = 'info') {
    const container = document.querySelector('.wa-toasts') || (() => {
        const c = document.createElement('div');
        c.className = 'toast-container position-fixed top-0 end-0 p-3 wa-toasts';
        c.style.zIndex = '9999';
        document.body.appendChild(c);
        return c;
    })();

    const toast = document.createElement('div');
    toast.className = `toast show align-items-center border-0 wa-toast wa-toast-${type}`;
    const wrap = document.createElement('div');
    wrap.className = 'd-flex';
    const body = document.createElement('div');
    body.className = 'toast-body';
    body.textContent = message;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close me-2 m-auto';
    close.setAttribute('data-bs-dismiss', 'toast');
    close.setAttribute('aria-label', 'Close');
    wrap.append(body, close);
    toast.appendChild(wrap);
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 4000);
}
