/**
 * List view — paginated job cards as alternative to map view.
 */
import { fetchJSON, escapeHTML, formatDate, pluralJobs } from './api.js';
import { getFilterParams } from './filters.js';
import { openDetail } from './detail.js';

const mapEl = document.getElementById('map');
const listPanel = document.getElementById('list-panel');
const listBody = document.getElementById('list-body');
const listCount = document.getElementById('list-count');
const listPagination = document.getElementById('list-pagination');
const jobCountEl = document.getElementById('job-count');
const mapBtn = document.getElementById('view-map');
const listBtn = document.getElementById('view-list');
const sortField = document.getElementById('list-sort-field');
const sortOrderBtn = document.getElementById('list-sort-order');
const filterPanel = document.getElementById('filter-panel');
const listSort = document.getElementById('list-sort');
const federalBtn = document.getElementById('source-federal');
const commercialBtn = document.getElementById('source-commercial');
const commercialSearch = document.getElementById('commercial-search');
const COMMERCIAL_PER_PAGE = 25;
const SEARCH_DEBOUNCE_MS = 300;

let currentPage = 1;
let sortOrder = 'desc';
let active = false;
let mapBbox = null;
let source = 'federal';
let searchTimer = null;

// Toggle between views
mapBtn.addEventListener('click', () => {
    active = false;
    mapEl.style.display = '';
    listPanel.style.display = 'none';
    jobCountEl.style.display = '';
    mapBtn.classList.add('active');
    listBtn.classList.remove('active');
});

listBtn.addEventListener('click', () => {
    active = true;
    // Keep the map rendered (sized) underneath the panel so its bounds stay valid
    listPanel.style.display = 'flex';
    jobCountEl.style.display = 'none';
    mapBtn.classList.remove('active');
    listBtn.classList.add('active');
    currentPage = 1;
    loadList();
});

federalBtn.addEventListener('click', () => setSource('federal'));
commercialBtn.addEventListener('click', () => setSource('commercial'));

// Commercial (ClearanceJobs) is a list-only source: only the free-text search
// applies, so the federal filters and sort controls are dimmed/hidden.
function setSource(next) {
    if (source === next) return;
    source = next;
    const commercial = next === 'commercial';
    federalBtn.classList.toggle('active', !commercial);
    commercialBtn.classList.toggle('active', commercial);
    filterPanel.classList.toggle('source-disabled', commercial);
    listSort.style.display = commercial ? 'none' : '';
    commercialSearch.style.display = commercial ? '' : 'none';
    currentPage = 1;
    loadList();
}

commercialSearch.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
        currentPage = 1;
        loadList();
    }, SEARCH_DEBOUNCE_MS);
});

sortField.addEventListener('change', () => {
    currentPage = 1;
    loadList();
});

sortOrderBtn.addEventListener('click', () => {
    sortOrder = sortOrder === 'asc' ? 'desc' : 'asc';
    sortOrderBtn.innerHTML = sortOrder === 'asc' ? '&darr;' : '&uarr;';
    currentPage = 1;
    loadList();
});

// Re-load list when filters change (listen for custom event from app.js)
window.addEventListener('filters-changed', () => {
    if (active) {
        currentPage = 1;
        loadList();
    }
});

// Keep the list scoped to the current map viewport (event from app.js on map move)
window.addEventListener('map-moved', (e) => {
    mapBbox = e.detail;
    if (active) {
        currentPage = 1;
        loadList();
    }
});

async function loadList() {
    if (source === 'commercial') return loadCommercial();

    const filterStr = getFilterParams();
    let url = `/api/jobs/list?page=${currentPage}&per_page=25&sort=${sortField.value}&order=${sortOrder}`;
    if (mapBbox) url += `&bbox=${mapBbox}`;
    if (filterStr) url += `&${filterStr}`;

    listBody.innerHTML = '<div class="list-loading">Loading...</div>';

    try {
        const data = await fetchJSON(url, { key: 'list' });
        renderList(data);
    } catch (err) {
        if (err.name === 'AbortError') return;
        listBody.innerHTML = `<div class="list-loading">Failed to load: ${escapeHTML(err.message)}</div>`;
    }
}

async function loadCommercial() {
    const params = new URLSearchParams({
        limit: COMMERCIAL_PER_PAGE,
        offset: (currentPage - 1) * COMMERCIAL_PER_PAGE,
    });
    const keyword = commercialSearch.value.trim();
    if (keyword) params.set('q', keyword);

    listBody.innerHTML = '<div class="list-loading">Loading...</div>';

    try {
        const data = await fetchJSON(`/api/commercial/jobs?${params}`, { key: 'list' });
        renderCommercial(data);
    } catch (err) {
        if (err.name === 'AbortError') return;
        listBody.innerHTML = `<div class="list-loading">Failed to load: ${escapeHTML(err.message)}</div>`;
    }
}

function renderCommercial(data) {
    listCount.textContent = pluralJobs(data.total);

    if (data.jobs.length === 0) {
        listBody.innerHTML = '<div class="list-empty">No commercial jobs match your search.</div>';
        listPagination.innerHTML = '';
        return;
    }

    listBody.innerHTML = '';
    for (const job of data.jobs) {
        const card = document.createElement('div');
        card.className = 'job-card';
        // No detail pane for commercial in P1 — open the ClearanceJobs posting directly
        card.addEventListener('click', () => window.open(job.url, '_blank', 'noopener'));

        const location = (job.locations && job.locations[0]) || 'N/A';
        const posted = formatDate(job.date_posted);

        card.innerHTML = `
            <div class="job-card-main">
                <div class="job-card-title">${escapeHTML(job.title)}</div>
                <div class="job-card-org">${escapeHTML(job.company || '')}</div>
            </div>
            <div class="job-card-details">
                <div class="job-card-meta">
                    <span class="job-meta-item job-meta-location">${escapeHTML(location)}</span>
                    ${job.industry ? `<span class="job-meta-item">${escapeHTML(job.industry)}</span>` : ''}
                    ${job.clearance ? `<span class="job-meta-item job-meta-clearance">${escapeHTML(job.clearance)}</span>` : ''}
                </div>
                <div class="job-card-footer">
                    <span class="job-card-close">${posted ? `Posted ${posted}` : ''}</span>
                </div>
            </div>
        `;
        listBody.appendChild(card);
    }

    const pages = data.total ? Math.ceil(data.total / (data.limit || COMMERCIAL_PER_PAGE)) : 0;
    renderPagination({ page: currentPage, pages });
}

function renderList(data) {
    listCount.textContent = pluralJobs(data.total);

    if (data.jobs.length === 0) {
        listBody.innerHTML = '<div class="list-empty">No jobs match your filters.</div>';
        listPagination.innerHTML = '';
        return;
    }

    listBody.innerHTML = '';
    for (const job of data.jobs) {
        const card = document.createElement('div');
        card.className = 'job-card';
        card.addEventListener('click', () => openDetail(job.id));

        const badges = [];
        if (job.remote) badges.push('<span class="job-badge job-badge-remote">Remote</span>');
        if (job.telework && !job.remote) badges.push('<span class="job-badge job-badge-telework">Telework</span>');

        const closeDate = formatDate(job.close_date);
        const closeSoon = isClosingSoon(job.close_date);

        card.innerHTML = `
            <div class="job-card-main">
                <div class="job-card-title">${escapeHTML(job.title)}</div>
                <div class="job-card-org">${escapeHTML(job.org)}</div>
                <div class="job-card-dept">${escapeHTML(job.department || '')}</div>
            </div>
            <div class="job-card-details">
                <div class="job-card-meta">
                    ${job.salary ? `<span class="job-meta-item job-meta-salary">${escapeHTML(job.salary)}</span>` : ''}
                    ${job.grade ? `<span class="job-meta-item job-meta-grade">${escapeHTML(job.grade)}</span>` : ''}
                    <span class="job-meta-item job-meta-location">${escapeHTML(job.location || 'N/A')}</span>
                    ${job.series ? `<span class="job-meta-item job-meta-series">${escapeHTML(job.series)}</span>` : ''}
                    ${job.clearance ? `<span class="job-meta-item job-meta-clearance">${escapeHTML(job.clearance)}</span>` : ''}
                </div>
                <div class="job-card-footer">
                    <span class="job-card-close${closeSoon ? ' closing-soon' : ''}">${closeDate ? `Closes ${closeDate}` : ''}</span>
                    <span class="job-card-badges">${badges.join('')}</span>
                </div>
            </div>
        `;
        listBody.appendChild(card);
    }

    renderPagination(data);
}

function renderPagination(data) {
    listPagination.innerHTML = '';
    if (data.pages <= 1) return;

    const nav = document.createElement('div');
    nav.className = 'pagination';

    // Previous
    if (data.page > 1) {
        nav.appendChild(makePageBtn('\u2190 Prev', data.page - 1));
    }

    // Page numbers
    const range = getPageRange(data.page, data.pages);
    for (const p of range) {
        if (p === '...') {
            const dots = document.createElement('span');
            dots.className = 'page-dots';
            dots.textContent = '...';
            nav.appendChild(dots);
        } else {
            const btn = makePageBtn(p.toString(), p);
            if (p === data.page) btn.classList.add('active');
            nav.appendChild(btn);
        }
    }

    // Next
    if (data.page < data.pages) {
        nav.appendChild(makePageBtn('Next \u2192', data.page + 1));
    }

    listPagination.appendChild(nav);
}

function makePageBtn(label, page) {
    const btn = document.createElement('button');
    btn.className = 'page-btn';
    btn.textContent = label;
    btn.addEventListener('click', () => {
        currentPage = page;
        loadList();
        listPanel.scrollTop = 0;
    });
    return btn;
}

function getPageRange(current, total) {
    if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
    const pages = [];
    pages.push(1);
    if (current > 3) pages.push('...');
    for (let i = Math.max(2, current - 1); i <= Math.min(total - 1, current + 1); i++) {
        pages.push(i);
    }
    if (current < total - 2) pages.push('...');
    pages.push(total);
    return pages;
}

function isClosingSoon(iso) {
    if (!iso) return false;
    const d = new Date(iso);
    const now = new Date();
    const diff = (d - now) / (1000 * 60 * 60 * 24);
    return diff >= 0 && diff <= 7;
}
