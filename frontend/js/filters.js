/**
 * Filter panel — collapsible sidebar with dropdowns and inputs.
 * Calls onFilterChange callback when any filter changes.
 * Re-fetches filter options on each change so dropdowns reflect available values.
 */
import { fetchJSON } from './api.js';

let onFilterChange = null;
let onZoomTo = null;
let onZoomToPoint = null;
let debounceTimer = null;
const DEBOUNCE_MS = 400;

// Full data from API
let allCities = [];
let countryBounds = {};
let localityBounds = {};
let mapBbox = null;

// DOM refs
const panel = document.getElementById('filter-panel');
const toggleBtn = document.getElementById('filter-toggle');
const departmentSelect = document.getElementById('filter-department');
const agencySelect = document.getElementById('filter-agency');
const seriesSelect = document.getElementById('filter-series');
const clearanceSelect = document.getElementById('filter-clearance');
const localitySelect = document.getElementById('filter-locality');
const excludeNcrCheckbox = document.getElementById('filter-exclude-ncr');
const excludeRegistersCheckbox = document.getElementById('filter-exclude-registers');
const excludeProvidersCheckbox = document.getElementById('filter-exclude-providers');
const stateSelect = document.getElementById('filter-state');
const countrySelect = document.getElementById('filter-country');
const citySelect = document.getElementById('filter-city');
const keywordInput = document.getElementById('filter-keyword');
const salaryMinInput = document.getElementById('filter-salary-min');
const salaryMaxInput = document.getElementById('filter-salary-max');
const gradeMinInput = document.getElementById('filter-grade-min');
const gradeMaxInput = document.getElementById('filter-grade-max');
const clearBtn = document.getElementById('filter-clear');

// State bounding boxes [south, west, north, east] from job location data
const STATE_BOUNDS = {
  "Alabama": [30.41, -88.24, 34.80, -85.01],
  "Alaska": [51.88, -176.66, 71.29, -131.65],
  "American Samoa": [-14.31, -170.74, -14.28, -170.70],
  "Arizona": [31.34, -114.78, 36.99, -109.08],
  "Arkansas": [33.21, -94.42, 36.42, -90.71],
  "California": [32.54, -124.20, 41.95, -114.38],
  "Colorado": [37.20, -108.74, 40.58, -102.27],
  "Connecticut": [41.11, -73.50, 42.00, -71.92],
  "Delaware": [38.69, -75.76, 39.78, -75.39],
  "District of Columbia": [38.82, -77.04, 38.97, -76.98],
  "Florida": [24.55, -87.30, 41.33, -74.36],
  "Georgia": [30.73, -85.35, 34.87, -81.09],
  "Guam": [13.38, 144.66, 13.57, 144.89],
  "Hawaii": [19.30, -159.55, 22.05, -155.08],
  "Idaho": [42.56, -117.04, 49.00, -111.45],
  "Illinois": [37.73, -91.40, 42.47, -87.63],
  "Indiana": [37.98, -87.56, 41.75, -84.97],
  "Iowa": [40.81, -96.42, 43.30, -90.58],
  "Kansas": [37.03, -101.71, 39.83, -94.63],
  "Kentucky": [36.61, -88.63, 39.08, -82.54],
  "Louisiana": [29.60, -93.75, 33.24, -89.78],
  "Maine": [43.08, -70.78, 47.36, -67.00],
  "Maryland": [38.08, -78.93, 39.66, -75.22],
  "Massachusetts": [41.29, -73.25, 42.80, -70.03],
  "Michigan": [41.83, -90.16, 48.00, -82.43],
  "Minnesota": [43.65, -96.32, 48.91, -89.68],
  "Mississippi": [30.31, -91.40, 34.96, -88.43],
  "Missouri": [36.64, -94.83, 40.35, -89.54],
  "Montana": [45.31, -115.05, 48.99, -104.16],
  "Nebraska": [40.59, -103.88, 42.69, -95.89],
  "Nevada": [35.17, -119.96, 41.96, -114.12],
  "New Hampshire": [42.73, -72.28, 44.47, -70.77],
  "New Jersey": [39.24, -75.31, 40.95, -73.99],
  "New Mexico": [31.83, -108.89, 36.93, -103.20],
  "New York": [40.56, -79.24, 44.99, -72.64],
  "North Carolina": [33.89, -83.30, 36.52, -75.67],
  "North Dakota": [46.09, -103.62, 49.00, -96.78],
  "Northern Mariana Islands": [15.19, 145.76, 15.19, 145.76],
  "Ohio": [38.65, -84.54, 41.77, -80.61],
  "Oklahoma": [33.99, -101.48, 36.87, -94.79],
  "Oregon": [42.01, -124.22, 46.19, -117.23],
  "Pennsylvania": [39.79, -80.45, 42.15, -75.13],
  "Puerto Rico": [18.01, -67.19, 18.47, -65.44],
  "Rhode Island": [41.48, -71.52, 41.82, -71.29],
  "South Carolina": [32.15, -82.65, 35.07, -78.73],
  "South Dakota": [42.83, -103.60, 45.81, -96.66],
  "Tennessee": [35.05, -90.05, 36.63, -82.22],
  "Texas": [25.90, -106.59, 35.65, -93.85],
  "Utah": [37.05, -113.67, 41.74, -109.18],
  "Vermont": [42.85, -73.21, 45.01, -71.51],
  "Virginia": [36.59, -83.11, 39.20, -75.37],
  "Virgin Islands": [17.74, -64.94, 18.38, -64.52],
  "Washington": [45.63, -124.63, 49.00, -117.05],
  "West Virginia": [37.43, -82.56, 40.06, -77.74],
  "Wisconsin": [42.53, -92.80, 46.81, -87.54],
  "Wyoming": [41.13, -110.96, 44.84, -104.74],
};

/** Initialize filters — load options and wire events */
export async function initFilters({ onFilter, onZoom, onZoomPoint }) {
    onFilterChange = onFilter;
    onZoomTo = onZoom;
    onZoomToPoint = onZoomPoint;

    // Toggle panel
    toggleBtn.addEventListener('click', () => {
        panel.classList.toggle('collapsed');
    });

    // Load initial (unfiltered) options
    await refreshFilterOptions();

    // Wire change events
    for (const sel of [departmentSelect, agencySelect, seriesSelect, clearanceSelect]) {
        sel.addEventListener('change', onFilterUpdated);
    }
    localitySelect.addEventListener('change', onLocalityChange);
    excludeNcrCheckbox.addEventListener('change', onFilterUpdated);
    excludeRegistersCheckbox.addEventListener('change', onFilterUpdated);
    excludeProvidersCheckbox.addEventListener('change', onFilterUpdated);
    stateSelect.addEventListener('change', onStateChange);
    countrySelect.addEventListener('change', onCountryChange);
    citySelect.addEventListener('change', onCityChange);
    for (const input of [keywordInput, salaryMinInput, salaryMaxInput, gradeMinInput, gradeMaxInput]) {
        input.addEventListener('input', onInputUpdated);
    }

    // Clear all
    clearBtn.addEventListener('click', clearFilters);

    // Re-scope the dropdown counts to the map viewport as it moves
    window.addEventListener('map-moved', (e) => {
        mapBbox = e.detail;
        refreshFilterOptions();
    });
}

/** Get current filter values as query params */
export function getFilterParams() {
    const params = new URLSearchParams();
    if (departmentSelect.value) params.set('department', departmentSelect.value);
    if (agencySelect.value) params.set('agency', agencySelect.value);
    if (seriesSelect.value) params.set('series', seriesSelect.value);
    if (clearanceSelect.value) params.set('clearance', clearanceSelect.value);
    if (localitySelect.value) params.set('locality', localitySelect.value);
    if (excludeNcrCheckbox.checked) params.set('exclude_ncr', 'true');
    if (excludeRegistersCheckbox.checked) params.set('exclude_registers', 'true');
    if (excludeProvidersCheckbox.checked) params.set('exclude_providers', 'true');
    if (stateSelect.value) params.set('state', stateSelect.value);
    if (countrySelect.value) params.set('country', countrySelect.value);
    if (citySelect.value) params.set('city', citySelect.value);
    if (keywordInput.value.trim()) params.set('keyword', keywordInput.value.trim());
    if (salaryMinInput.value) params.set('salary_min', salaryMinInput.value);
    if (salaryMaxInput.value) params.set('salary_max', salaryMaxInput.value);
    if (gradeMinInput.value) params.set('grade_min', gradeMinInput.value);
    if (gradeMaxInput.value) params.set('grade_max', gradeMaxInput.value);
    return params.toString();
}

/** Fetch filter options from API (optionally filtered by current selections) */
async function refreshFilterOptions() {
    const filterStr = getFilterParams();
    let url = '/api/filters';
    const qs = [];
    if (mapBbox) qs.push(`bbox=${mapBbox}`);
    if (filterStr) qs.push(filterStr);
    if (qs.length) url += `?${qs.join('&')}`;

    try {
        const data = await fetchJSON(url, { key: 'filters' });
        const withCount = (item) => ({ value: item.name, label: `${item.name} (${item.count.toLocaleString()})` });
        updateSelect(departmentSelect, data.departments.map(withCount));
        updateSelect(agencySelect, data.agencies.map(withCount));
        const seriesWithCount = (item) => ({ value: item.code, label: `${item.name} (${item.count.toLocaleString()})` });
        updateSelect(seriesSelect, (data.series || []).map(seriesWithCount));
        updateSelect(clearanceSelect, data.clearances.map(withCount));
        updateSelect(localitySelect, (data.localities || []).map(withCount));
        updateSelect(stateSelect, data.states.map(withCount));
        updateSelect(countrySelect, data.countries.map(withCount));

        allCities = data.cities || [];
        countryBounds = data.country_bounds || {};
        localityBounds = data.locality_bounds || {};
        populateCities();
    } catch (err) {
        if (err.name === 'AbortError') return; // Cancelled — ignore
        console.error('Failed to load filters:', err);
    }
}

function clearOptionsExceptFirst(select) {
    while (select.options.length > 1) select.remove(1);
}

function restoreSelection(select, previous) {
    select.value = previous && [...select.options].some(o => o.value === previous) ? previous : '';
}

/** Update a select's options while preserving the current selection */
function updateSelect(select, options) {
    const current = select.value;
    clearOptionsExceptFirst(select);
    for (const opt of options) {
        const el = document.createElement('option');
        el.value = opt.value;
        el.textContent = opt.label;
        select.appendChild(el);
    }
    restoreSelection(select, current);
}

/** Repopulate city dropdown based on current state/country selection */
function populateCities() {
    const current = citySelect.value;
    clearOptionsExceptFirst(citySelect);

    const selectedState = stateSelect.value;
    const selectedCountry = countrySelect.value;

    let filtered = allCities;
    if (selectedState) {
        filtered = filtered.filter(c => c.state === selectedState);
    }
    if (selectedCountry) {
        filtered = filtered.filter(c => c.country === selectedCountry);
    }

    // Sort by count descending
    filtered.sort((a, b) => b.count - a.count);

    for (const city of filtered) {
        const el = document.createElement('option');
        el.value = city.name;
        el.textContent = `${city.name} (${city.count})`;
        el.dataset.lat = city.lat;
        el.dataset.lon = city.lon;
        citySelect.appendChild(el);
    }

    restoreSelection(citySelect, current);
}

/** Called when a dropdown filter changes */
function onFilterUpdated() {
    refreshFilterOptions();
    fireChange();
}

/** Called when text/number inputs change (debounced) */
function onInputUpdated() {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
        refreshFilterOptions();
        fireChange();
    }, DEBOUNCE_MS);
}

function onStateChange() {
    const state = stateSelect.value;
    const bounds = STATE_BOUNDS[state];
    refreshFilterOptions();
    if (bounds && onZoomTo) {
        onZoomTo(bounds);
    } else {
        fireChange();
    }
}

function onCountryChange() {
    const country = countrySelect.value;
    const bounds = countryBounds[country];
    refreshFilterOptions();
    if (bounds && onZoomTo) {
        onZoomTo(bounds);
    } else {
        fireChange();
    }
}

function onLocalityChange() {
    const bounds = localityBounds[localitySelect.value];
    refreshFilterOptions();
    if (bounds && onZoomTo) {
        onZoomTo(bounds);
    } else {
        fireChange();
    }
}

function onCityChange() {
    const selected = citySelect.options[citySelect.selectedIndex];
    refreshFilterOptions();
    if (selected && selected.dataset.lat && onZoomToPoint) {
        const lat = parseFloat(selected.dataset.lat);
        const lon = parseFloat(selected.dataset.lon);
        onZoomToPoint(lat, lon, 12);
    } else {
        fireChange();
    }
}

function fireChange() {
    if (onFilterChange) onFilterChange();
}

function clearFilters() {
    departmentSelect.value = '';
    agencySelect.value = '';
    seriesSelect.value = '';
    clearanceSelect.value = '';
    localitySelect.value = '';
    excludeNcrCheckbox.checked = false;
    excludeRegistersCheckbox.checked = true;
    excludeProvidersCheckbox.checked = false;
    stateSelect.value = '';
    countrySelect.value = '';
    citySelect.value = '';
    keywordInput.value = '';
    salaryMinInput.value = '';
    salaryMaxInput.value = '';
    gradeMinInput.value = '';
    gradeMaxInput.value = '';
    refreshFilterOptions();
    fireChange();
}
