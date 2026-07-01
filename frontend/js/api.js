/**
 * API fetch wrapper with AbortController support.
 * Cancels in-flight requests when a new one is made for the same key.
 */
const controllers = {};

export async function fetchJSON(url, { key = 'default' } = {}) {
    // Cancel any in-flight request for the same key
    if (controllers[key]) {
        controllers[key].abort();
    }

    const controller = new AbortController();
    controllers[key] = controller;

    const resp = await fetch(url, { signal: controller.signal });

    // Clean up controller reference
    if (controllers[key] === controller) {
        delete controllers[key];
    }

    if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${resp.status}`);
    }

    return resp.json();
}

export function escapeHTML(str) {
    if (!str) return '';
    const el = document.createElement('span');
    el.textContent = str;
    return el.innerHTML;
}

export function formatDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

export function pluralJobs(n) {
    return `${n.toLocaleString()} job${n !== 1 ? 's' : ''}`;
}
