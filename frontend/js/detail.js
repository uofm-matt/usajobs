/**
 * Detail panel — slide-in right panel showing full job info.
 * All content rendered via textContent (no innerHTML) for XSS safety.
 */
import { fetchJSON, formatDate } from './api.js';

const TRAVEL_LABELS = { '0': 'Not required', '1': 'Occasional travel', '2': '25% or less', '3': '50% or greater', '5': '50% or less', '7': '75% or less', '8': '76% or greater' };

const panel = document.getElementById('detail-panel');
const closeBtn = document.getElementById('detail-close');

// Field elements
const titleEl = document.getElementById('detail-title');
const orgEl = document.getElementById('detail-org');
const deptEl = document.getElementById('detail-dept');
const salaryEl = document.getElementById('detail-salary');
const gradeEl = document.getElementById('detail-grade');
const locationEl = document.getElementById('detail-location');
const clearanceEl = document.getElementById('detail-clearance');
const datesEl = document.getElementById('detail-dates');
const travelEl = document.getElementById('detail-travel');
const teleworkEl = document.getElementById('detail-telework');
const promoEl = document.getElementById('detail-promo');
const hiringEl = document.getElementById('detail-hiring');
const summaryEl = document.getElementById('detail-summary');
const dutiesEl = document.getElementById('detail-duties');
const educationEl = document.getElementById('detail-education');
const requirementsEl = document.getElementById('detail-requirements');
const evaluationsEl = document.getElementById('detail-evaluations');
const howToApplyEl = document.getElementById('detail-how-to-apply');
const applyBtn = document.getElementById('detail-apply');
const viewBtn = document.getElementById('detail-view');
const loadingEl = document.getElementById('detail-loading');
const contentEl = document.getElementById('detail-content');

closeBtn.addEventListener('click', close);

/** Open detail panel and fetch job data */
export async function openDetail(positionId) {
    panel.classList.add('open');
    loadingEl.style.display = 'block';
    contentEl.style.display = 'none';

    try {
        const job = await fetchJSON(`/api/jobs/${encodeURIComponent(positionId)}`, { key: 'detail' });
        render(job);
    } catch (err) {
        if (err.name === 'AbortError') return;
        titleEl.textContent = 'Error loading job details';
        orgEl.textContent = err.message;
        loadingEl.style.display = 'none';
        contentEl.style.display = 'block';
    }
}

function close() {
    panel.classList.remove('open');
}

function render(job) {
    titleEl.textContent = job.title;
    orgEl.textContent = job.org;
    deptEl.textContent = job.department;
    salaryEl.textContent = job.salary || 'Not specified';
    gradeEl.textContent = job.grade || 'N/A';
    locationEl.textContent = job.locations.join('; ') || 'N/A';
    clearanceEl.textContent = job.clearance || 'Not specified';

    // Format dates
    const openDate = formatDate(job.open_date);
    const closeDate = formatDate(job.close_date);
    datesEl.textContent = openDate && closeDate ? `${openDate} – ${closeDate}` : 'N/A';

    travelEl.textContent = TRAVEL_LABELS[job.travel] || 'Not specified';
    teleworkEl.textContent = typeof job.telework === 'boolean' ? (job.telework ? 'Yes' : 'No') : 'Not specified';
    promoEl.textContent = job.promotion_potential || 'N/A';
    hiringEl.textContent = (job.hiring_paths || []).join(', ') || 'N/A';

    // Long-form sections
    setSection(summaryEl, job.summary);
    setSection(dutiesEl, job.duties);
    setSection(educationEl, job.education);
    setSection(requirementsEl, job.requirements);
    setSection(evaluationsEl, job.evaluations);
    setSection(howToApplyEl, job.how_to_apply);

    // Apply button
    if (job.apply_url) {
        applyBtn.href = job.apply_url;
        applyBtn.style.display = 'inline-block';
    } else {
        applyBtn.style.display = 'none';
    }

    // View on USAJobs button
    if (job.usajobs_url) {
        viewBtn.href = job.usajobs_url;
        viewBtn.style.display = 'inline-block';
    } else {
        viewBtn.style.display = 'none';
    }

    loadingEl.style.display = 'none';
    contentEl.style.display = 'block';
}

function setSection(el, text) {
    const parent = el.closest('.detail-section');
    if (text && text.trim()) {
        el.textContent = text;
        parent.style.display = 'block';
    } else {
        parent.style.display = 'none';
    }
}
