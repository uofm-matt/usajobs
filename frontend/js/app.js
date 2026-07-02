/**
 * USAJobs Map — main application.
 * Leaflet map with Google Maps tiles, loads job markers from API on viewport change.
 */
import { fetchJSON, escapeHTML, pluralJobs } from './api.js';
import { openDetail } from './detail.js';
import { initFilters, getFilterParams } from './filters.js';

let map;
let jobLayer;
let debounceTimer;
const DEBOUNCE_MS = 300;

// DOM elements
const loadingEl = document.getElementById('loading');
const jobCountEl = document.getElementById('job-count');
const toastEl = document.getElementById('toast');

/** Show/hide loading indicator */
function setLoading(active) {
    loadingEl.classList.toggle('active', active);
}

/** Show error toast, auto-dismiss after 4s */
function showToast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add('visible');
    setTimeout(() => toastEl.classList.remove('visible'), 4000);
}

/** Initialize the Leaflet map */
async function initMap() {
    // Fetch config for Google Maps API key
    let config = {};
    try {
        config = await fetchJSON('/api/config', { key: 'config' });
    } catch {
        // Config fetch failed — will fall back to OSM tiles
    }

    map = L.map('map', {
        center: [39.8, -98.5], // Center of CONUS
        zoom: 5,
        zoomControl: true,
    });

    // Use Google Maps tiles if API key is available, otherwise CartoDB/OSM
    const addFallbackTiles = () => L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
        maxZoom: 19,
    }).addTo(map);

    if (config.google_maps_api_key) {
        try {
            await loadScript(`https://maps.googleapis.com/maps/api/js?key=${config.google_maps_api_key}`);
            await loadScript('https://unpkg.com/leaflet.gridlayer.googlemutant@0.14.1/dist/Leaflet.GoogleMutant.js');
            L.gridLayer.googleMutant({ type: 'roadmap' }).addTo(map);
        } catch {
            addFallbackTiles();
        }
    } else {
        addFallbackTiles();
    }

    // Layer for job markers
    jobLayer = L.layerGroup().addTo(map);

    // Load jobs on viewport change
    map.on('moveend', () => {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(loadJobs, DEBOUNCE_MS);
    });

    // Initialize filter panel — reload jobs when filters change
    await initFilters({
        onFilter: () => loadJobs(true),
        onZoom: ([south, west, north, east]) => {
            map.fitBounds([[south, west], [north, east]], { padding: [20, 20] });
        },
        onZoomPoint: (lat, lon, zoom) => {
            map.setView([lat, lon], zoom);
        },
    });

    // Initial load
    loadJobs();
}

/** Fetch jobs for current viewport and render on map */
async function loadJobs(fromFilter = false) {
    if (fromFilter) {
        window.dispatchEvent(new Event('filters-changed'));
    }
    const bounds = map.getBounds();
    const bbox = [
        bounds.getWest().toFixed(6),
        bounds.getSouth().toFixed(6),
        bounds.getEast().toFixed(6),
        bounds.getNorth().toFixed(6),
    ].join(',');
    const zoom = map.getZoom();

    // Keep the list view scoped to the current map viewport
    if (!fromFilter) {
        window.dispatchEvent(new CustomEvent('map-moved', { detail: bbox }));
    }

    const filterParams = getFilterParams();
    let url = `/api/jobs?bbox=${bbox}&zoom=${zoom}`;
    if (filterParams) url += `&${filterParams}`;

    setLoading(true);

    try {
        const geojson = await fetchJSON(url, { key: 'jobs' });
        renderJobs(geojson);
    } catch (err) {
        if (err.name === 'AbortError') return; // Cancelled — superseding call owns the spinner
        showToast(`Failed to load jobs: ${err.message}`);
    }
    setLoading(false);
}

/** Render GeoJSON features on the map */
function renderJobs(geojson) {
    jobLayer.clearLayers();

    const meta = geojson.metadata || {};
    const total = meta.total ?? geojson.features.length;

    jobCountEl.textContent = `${pluralJobs(total)} in view`;

    for (const feature of geojson.features) {
        const [lon, lat] = feature.geometry.coordinates;
        const p = feature.properties;

        if (p.cluster) {
            renderCluster(lat, lon, p);
        } else {
            renderPoint(lat, lon, p);
        }
    }
}

/** Render a cluster marker — sized by log(count), click to zoom in */
function renderCluster(lat, lon, p) {
    const count = p.point_count || 1;
    const radius = Math.max(10, Math.min(40, 6 + Math.log2(count) * 4));

    const marker = L.circleMarker([lat, lon], {
        radius,
        fillColor: '#e65100',
        fillOpacity: 0.65,
        color: '#bf360c',
        weight: 1.5,
    });

    // Count label as tooltip (permanent)
    const label = count >= 1000 ? `${(count / 1000).toFixed(1)}k` : count.toString();
    marker.bindTooltip(label, {
        permanent: true,
        direction: 'center',
        className: 'cluster-label',
    });

    // Click to zoom in
    marker.on('click', () => {
        map.setView([lat, lon], map.getZoom() + 3);
    });

    marker.addTo(jobLayer);
}

/** Render an individual job point */
function renderPoint(lat, lon, p) {
    const marker = L.circleMarker([lat, lon], {
        radius: 5,
        fillColor: '#1565c0',
        fillOpacity: 0.7,
        color: '#0d47a1',
        weight: 1,
    });

    marker.bindTooltip(
        `<strong>${escapeHTML(p.title)}</strong><br>${escapeHTML(p.org)}`,
        { direction: 'top', offset: [0, -6] }
    );

    marker.on('click', () => openDetail(p.id));

    marker.addTo(jobLayer);
}

/** Dynamically load an external script */
function loadScript(src) {
    return new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = src;
        s.onload = resolve;
        s.onerror = reject;
        document.head.appendChild(s);
    });
}

// Boot
initMap();
