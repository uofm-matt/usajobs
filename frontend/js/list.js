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
const federalFilters = document.getElementById('federal-filters');
const commercialFilters = document.getElementById('commercial-filters');
const federalBtn = document.getElementById('source-federal');
const commercialBtn = document.getElementById('source-commercial');
const commercialSearch = document.getElementById('commercial-search');
const cjCompany = document.getElementById('cj-filter-company');
const cjCompanyOptions = document.getElementById('cj-company-options');
const cjLocation = document.getElementById('cj-filter-location');
const cjSalaryMin = document.getElementById('cj-filter-salary-min');
const cjPosted = document.getElementById('cj-filter-posted');
const cjExcludeNcr = document.getElementById('cj-filter-exclude-ncr');
const cjRemote = document.getElementById('cj-filter-remote');
const cjClearBtn = document.getElementById('cj-filter-clear');
const COMMERCIAL_PER_PAGE = 25;
const SEARCH_DEBOUNCE_MS = 300;

// Industry facet values that start unchecked (excluded by default). Facilities /
// construction roles are noise for a cleared-tech search; the user can re-check.
const CJ_DEFAULT_HIDDEN_INDUSTRIES = new Set(['Construction/Facilities']);
const cjDefaultChecked = (facet, value) =>
    !(facet.key === 'industry' && CJ_DEFAULT_HIDDEN_INDUSTRIES.has(value));

// Faceted checkbox groups. `param` is the repeated query-param name sent to
// /api/commercial/jobs; `field` keys into the /api/commercial/filters payload;
// `body` is the container the checkboxes render into (id cj-facet-<key>).
const CJ_FACETS = [
    { key: 'clearance', param: 'clearance', field: 'clearances' },
    { key: 'country', param: 'country', field: 'countries' },
    { key: 'industry', param: 'industry', field: 'industries' },
    { key: 'employment_type', param: 'employment_type', field: 'employment_types' },
    { key: 'loc', param: 'loc', field: 'locations' },
];
const cjFacetBodies = Object.fromEntries(
    CJ_FACETS.map((f) => [f.key, document.getElementById(`cj-facet-${f.key}`)]),
);

// Federal options come from the HTML; commercial swaps in its own set on source
// change. Keep the original markup and prior selection to restore on switch back.
const FEDERAL_SORT_OPTIONS = sortField.innerHTML;
const COMMERCIAL_SORT_OPTIONS = `
    <option value="posted" selected>Posted</option>
    <option value="close">Close Date</option>
    <option value="salary">Salary</option>
    <option value="title">Title</option>
    <option value="company">Company</option>
    <option value="clearance">Clearance</option>
    <option value="location">Location</option>`;

let currentPage = 1;
let sortOrder = 'desc';
let active = false;
let mapBbox = null;
let source = 'federal';
let searchTimer = null;
let cjFilterTimer = null;
let commercialFiltersLoaded = false;
let federalSortValue = sortField.value;

// Toggle between views. Commercial is a full map citizen now: the source toggle
// keeps app.js's layer in sync (see setSource), so this is a plain display toggle
// and the federal map is untouched — pixel-identical to before.
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

// Commercial (ClearanceJobs) and Federal (USAJobs) each own a filter section in
// the shared sidebar; switching source swaps which one is visible along with the
// sort option set.
function setSource(next) {
    if (source === next) return;
    source = next;
    const commercial = next === 'commercial';
    federalBtn.classList.toggle('active', !commercial);
    commercialBtn.classList.toggle('active', commercial);
    federalFilters.style.display = commercial ? 'none' : '';
    commercialFilters.style.display = commercial ? '' : 'none';
    commercialSearch.style.display = commercial ? '' : 'none';
    if (commercial) {
        federalSortValue = sortField.value;
        sortField.innerHTML = COMMERCIAL_SORT_OPTIONS;
        loadCommercialFilters();
        loadCompanyOptions();
    } else {
        sortField.innerHTML = FEDERAL_SORT_OPTIONS;
        sortField.value = federalSortValue;
    }
    currentPage = 1;
    loadList();
    // Keep app.js's map layer in step with the source — even in list view, so the
    // right markers are ready when the map is shown (and the source is never stale).
    reloadMap();
}

// Tell app.js (separate module, owns the Leaflet layer) to reload the map layer
// for the current source. The source rides along so app.js needn't track it.
function reloadMap() {
    window.dispatchEvent(new CustomEvent('maplayer-reload', { detail: source }));
}

// A commercial filter changed: reload the list when it's the active view, else
// the map layer. (The commercial sidebar shows in both views.)
function commercialChanged() {
    currentPage = 1;
    if (active) loadList();
    else reloadMap();
}

// Facet options for the commercial sidebar — fetched once per session, since the
// active-set counts drift slowly and don't warrant a refetch on every toggle.
async function loadCommercialFilters() {
    if (commercialFiltersLoaded) return;
    try {
        const data = await fetchJSON('/api/commercial/filters', { key: 'cj-filters' });
        for (const facet of CJ_FACETS) buildFacetGroup(facet, data[facet.field]);
        commercialFiltersLoaded = true;
    } catch (err) {
        if (err.name === 'AbortError') return;
        console.error('Failed to load commercial filters:', err);
    }
}

// Employer suggestions for the company box, ordered most-jobs-first and scoped to
// the current map viewport, so the dropdown reflects who is hiring on screen. The
// input stays free-text (ILIKE), so a typed partial still works if not picked.
async function loadCompanyOptions() {
    const params = new URLSearchParams();
    if (mapBbox) params.set('bbox', mapBbox);
    try {
        const data = await fetchJSON(`/api/commercial/companies?${params}`, {
            key: 'cj-companies',
        });
        cjCompanyOptions.innerHTML = '';
        for (const c of data.companies) {
            const opt = document.createElement('option');
            opt.value = c.value;
            opt.label = `${c.value} (${c.count.toLocaleString()})`;
            cjCompanyOptions.appendChild(opt);
        }
    } catch (err) {
        if (err.name === 'AbortError') return;
        console.error('Failed to load company options:', err);
    }
}

// Render one checkbox per facet value into the group body, all checked. Changes
// apply immediately (no debounce) and reset to page 1.
function buildFacetGroup(facet, options) {
    const body = cjFacetBodies[facet.key];
    body.innerHTML = '';
    for (const opt of options || []) {
        const label = document.createElement('label');
        label.className = 'cj-facet-option';
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = opt.value;
        cb.checked = cjDefaultChecked(facet, opt.value);
        cb.addEventListener('change', commercialChanged);
        const text = document.createElement('span');
        text.textContent = `${opt.value} (${opt.count.toLocaleString()})`;
        label.append(cb, text);
        body.appendChild(label);
    }
}

function facetBoxes(key) {
    return [...cjFacetBodies[key].querySelectorAll('input[type="checkbox"]')];
}

function setFacetGroup(key, checked) {
    for (const cb of facetBoxes(key)) cb.checked = checked;
    commercialChanged();
}

// "all | none" links per group live in the static HTML headers.
for (const group of document.querySelectorAll('.cj-facet-group')) {
    const key = group.dataset.facet;
    for (const link of group.querySelectorAll('.cj-facet-actions a')) {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            setFacetGroup(key, link.dataset.action === 'all');
        });
    }
}

commercialSearch.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(commercialChanged, SEARCH_DEBOUNCE_MS);
});

cjExcludeNcr.addEventListener('change', commercialChanged);
cjRemote.addEventListener('change', commercialChanged);
cjPosted.addEventListener('change', commercialChanged);

for (const input of [cjCompany, cjLocation, cjSalaryMin]) {
    input.addEventListener('input', () => {
        clearTimeout(cjFilterTimer);
        cjFilterTimer = setTimeout(commercialChanged, SEARCH_DEBOUNCE_MS);
    });
}

cjClearBtn.addEventListener('click', () => {
    cjCompany.value = '';
    cjLocation.value = '';
    cjSalaryMin.value = '';
    cjExcludeNcr.checked = false;
    cjRemote.checked = false;
    cjPosted.value = '';
    commercialSearch.value = '';
    for (const facet of CJ_FACETS) {
        for (const cb of facetBoxes(facet.key)) {
            cb.checked = cjDefaultChecked(facet, cb.value);
        }
    }
    commercialChanged();
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
    // Refresh the employer dropdown to who's hiring in the new viewport.
    if (source === 'commercial') loadCompanyOptions();
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

// Append the active commercial filter values (search box, company, location,
// salary floor, NCR toggle, and each facet group's selection) onto `params`.
// Per group: all checked (or not yet loaded) omits the param; a subset appends one
// repeated param per checked value; none checked returns false so the caller can
// render the empty state (list) or clear the markers (map) — an omitted param
// would wrongly read as "all".
function appendCommercialFilters(params) {
    const keyword = commercialSearch.value.trim();
    if (keyword) params.set('q', keyword);
    if (cjCompany.value.trim()) params.set('company', cjCompany.value.trim());
    if (cjLocation.value.trim()) params.set('location', cjLocation.value.trim());
    if (cjSalaryMin.value) params.set('salary_min', cjSalaryMin.value);
    if (cjExcludeNcr.checked) params.set('exclude_ncr', '1');
    if (cjRemote.checked) params.set('remote', '1');
    if (cjPosted.value) params.set('max_age_days', cjPosted.value);

    for (const facet of CJ_FACETS) {
        const boxes = facetBoxes(facet.key);
        if (!boxes.length) continue;
        const checked = boxes.filter((b) => b.checked);
        if (checked.length === boxes.length) continue;
        if (!checked.length) return false;
        for (const b of checked) params.append(facet.param, b.value);
    }
    return true;
}

// Bridge for app.js (separate module): the /api/commercial/map filter query for
// the current sidebar state, or null when a facet group is fully unchecked (app.js
// clears the markers rather than querying).
window.commercialMapQuery = () => {
    const params = new URLSearchParams();
    return appendCommercialFilters(params) ? params.toString() : null;
};

async function loadCommercial() {
    const params = new URLSearchParams({
        limit: COMMERCIAL_PER_PAGE,
        offset: (currentPage - 1) * COMMERCIAL_PER_PAGE,
        sort: sortField.value,
        order: sortOrder,
    });
    if (!appendCommercialFilters(params)) {
        renderCommercial({ total: 0, jobs: [] });
        return;
    }
    // Scope the list to the current map viewport, so zooming the map and returning
    // to the list narrows it to what's on screen (mirrors the federal list).
    if (mapBbox) params.set('bbox', mapBbox);

    listBody.innerHTML = '<div class="list-loading">Loading...</div>';

    try {
        const data = await fetchJSON(`/api/commercial/jobs?${params}`, { key: 'list' });
        renderCommercial(data);
    } catch (err) {
        if (err.name === 'AbortError') return;
        listBody.innerHTML = `<div class="list-loading">Failed to load: ${escapeHTML(err.message)}</div>`;
    }
}

// Commercial salaries arrive as bare integers (annual USD), unlike the
// pre-formatted federal salary string.
function formatSalaryRange(min, max) {
    const fmt = (n) => `$${n.toLocaleString()}`;
    if (min && max) return `${fmt(min)}–${fmt(max)}`;
    if (min || max) return fmt(min || max);
    return '';
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
        const salary = formatSalaryRange(job.salary_min, job.salary_max);

        card.innerHTML = `
            <div class="job-card-main">
                <div class="job-card-title">${escapeHTML(job.title)}</div>
                <div class="job-card-org">${escapeHTML(job.company || '')}</div>
            </div>
            <div class="job-card-details">
                <div class="job-card-meta">
                    ${salary ? `<span class="job-meta-item job-meta-salary">${salary}</span>` : ''}
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
