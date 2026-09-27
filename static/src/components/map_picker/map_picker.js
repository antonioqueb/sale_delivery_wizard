/** @odoo-module **/
// Selector de ubicación en mapa (Leaflet vendorizado del módulo).
//
// Widget de campo para la LATITUD; escribe también la LONGITUD (option
// lng_field) y, al elegir una dirección de la lista, la DIRECCIÓN DE
// ENTREGA (option address_field). Clic o arrastre del marcador fija el
// punto; "Mi ubicación" usa el GPS del navegador.
//
// 21 sep 2026 — mapa "a priori":
// - El mapa se pinta aunque el contenedor nazca sin tamaño (registro nuevo
//   que se abre desde «Programar entrega»): se reintenta la inicialización
//   en cada patch y un ResizeObserver redibuja en cuanto el contenedor mide.
// - Sin punto pero con dirección, el widget geocodifica solo al abrir y
//   cada vez que cambia el texto de la dirección (mientras el vendedor no
//   haya fijado el punto a mano).
// - Búsqueda en vivo: al teclear en el buscador salen las coincidencias
//   sin pulsar ningún botón; al elegir una, el punto se fija y la dirección
//   completa se pega en el campo Dirección de entrega (antes ambos campos
//   vivían aislados).
// - Marcador propio (SVG inline): el ícono por defecto de Leaflet buscaba
//   marker-icon.png junto al CSS y en el bundle de Odoo no existe → salía
//   el ícono de imagen rota.
//
// 27 sep 2026 — relación CONTROLADA dirección ⇄ mapa:
// - Dirección → mapa: al cambiar la dirección de entrega el mapa la vuelve
//   a localizar (aunque el punto se hubiera fijado a mano antes).
// - Mapa → dirección: NUNCA. Buscar en el mapa, pegar coordenadas, mover o
//   hacer clic solo guarda latitud/longitud; la dirección comercial
//   capturada queda intacta (antes la búsqueda la reemplazaba por la del
//   proveedor de mapas).
export const COORDS_RE = /^\s*(-?\d{1,2}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)\s*$/;

/** "25.68, -100.31" → [25.68, -100.31] si son coordenadas válidas. */
export function parseCoords(text) {
    const m = COORDS_RE.exec(text || "");
    if (!m) {
        return null;
    }
    const lat = parseFloat(m[1]);
    const lng = parseFloat(m[2]);
    if (Math.abs(lat) > 90 || Math.abs(lng) > 180) {
        return null;
    }
    return [lat, lng];
}
import { Component, onMounted, onPatched, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

const DEFAULT_CENTER = [25.6866, -100.3161]; // Monterrey
// OpenStreetMap sin subdominios (a/b/c están en desuso en su servidor).
const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const AUTO_GEOCODE_DELAY = 900; // ms sin teclear en la dirección antes de estimar
const LIVE_SEARCH_DELAY = 450; // ms sin teclear en el buscador antes de consultar
const MIN_ADDRESS_LEN = 10;
const MIN_QUERY_LEN = 4;
const PIN_SVG =
    '<svg xmlns="http://www.w3.org/2000/svg" width="30" height="42" viewBox="0 0 30 42">' +
    '<path d="M15 1C7.3 1 1 7.2 1 14.9c0 9.9 12.1 24.6 13 25.6a1.3 1.3 0 0 0 2 0c.9-1 13-15.7 13-25.6C29 7.2 22.7 1 15 1z" ' +
    'fill="#B3261E" stroke="#fff" stroke-width="2"/>' +
    '<circle cx="15" cy="15" r="5.5" fill="#fff"/></svg>';

export class SomMapPicker extends Component {
    static template = "sale_delivery_wizard.SomMapPicker";
    static props = {
        ...standardFieldProps,
        lngField: { type: String, optional: true },
        addressField: { type: String, optional: true },
    };

    setup() {
        this.notification = useService("notification");
        this.mapRef = useRef("map");
        this.state = useState({ searching: false, query: "", results: [], autoNote: "" });
        this.map = null;
        this.marker = null;
        this.resizeObserver = null;
        // true mientras el punto venga de la geocodificación automática (o no
        // haya punto): el mapa sigue a la dirección. Se apaga en cuanto el
        // vendedor fija el punto a mano (clic, arrastre, lista, GPS).
        this.pointIsAuto = !this.hasPoint;
        this.lastGeocoded = "";
        this.seenAddress = null;
        this.geocodeTimer = null;
        this.liveTimer = null;
        this.liveSeq = 0;

        onMounted(() => {
            this.ensureMap();
            this.scheduleAutoGeocode(0);
        });
        onPatched(() => this.ensureMap());
        onWillUnmount(() => {
            clearTimeout(this.geocodeTimer);
            clearTimeout(this.liveTimer);
            if (this.resizeObserver) {
                this.resizeObserver.disconnect();
                this.resizeObserver = null;
            }
            if (this.map) {
                this.map.remove();
                this.map = null;
            }
        });
        useEffect(
            () => this.syncMarker(),
            () => [this.lat, this.lng]
        );
        useEffect(
            () => {
                const text = (this.addressText || "").trim();
                // La dirección CAMBIÓ (no es la carga inicial): el mapa la
                // sigue aunque el punto se hubiera fijado a mano.
                if (this.seenAddress !== null && text !== this.seenAddress) {
                    this.pointIsAuto = true;
                }
                this.seenAddress = text;
                this.scheduleAutoGeocode(AUTO_GEOCODE_DELAY);
            },
            () => [this.addressText]
        );
    }

    get lngField() {
        return this.props.lngField || "longitude";
    }

    get lat() {
        return this.props.record.data[this.props.name] || 0;
    }

    get lng() {
        return this.props.record.data[this.lngField] || 0;
    }

    get hasPoint() {
        return Boolean(this.lat && this.lng);
    }

    get addressText() {
        return this.props.addressField ? this.props.record.data[this.props.addressField] || "" : "";
    }

    get readonly() {
        return this.props.readonly;
    }

    // ------------------------------------------------------------------
    // Mapa
    // ------------------------------------------------------------------
    ensureMap() {
        if (this.map || !this.mapRef.el || !window.L) {
            return;
        }
        this.initMap();
    }

    pinIcon() {
        return window.L.divIcon({
            className: "o_smp_pin",
            html: PIN_SVG,
            iconSize: [30, 42],
            iconAnchor: [15, 41],
        });
    }

    initMap() {
        const L = window.L;
        if (!L || !this.mapRef.el) {
            return;
        }
        const center = this.hasPoint ? [this.lat, this.lng] : DEFAULT_CENTER;
        this.map = L.map(this.mapRef.el, { center, zoom: this.hasPoint ? 16 : 11, zoomControl: true });
        L.tileLayer(TILES, {
            maxZoom: 19,
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>',
        }).addTo(this.map);
        if (this.hasPoint) {
            this.placeMarker(this.lat, this.lng, false);
        }
        if (!this.readonly) {
            this.map.on("click", (ev) => this.setPoint(ev.latlng.lat, ev.latlng.lng, { manual: true }));
        }
        if (window.ResizeObserver) {
            this.resizeObserver = new ResizeObserver(() => {
                if (this.map) {
                    this.map.invalidateSize();
                }
            });
            this.resizeObserver.observe(this.mapRef.el);
        }
        setTimeout(() => this.map && this.map.invalidateSize(), 200);
        setTimeout(() => this.map && this.map.invalidateSize(), 800);
    }

    placeMarker(lat, lng, pan = true) {
        const L = window.L;
        if (!this.map) {
            return;
        }
        if (!this.marker) {
            this.marker = L.marker([lat, lng], { draggable: !this.readonly, icon: this.pinIcon() }).addTo(this.map);
            if (!this.readonly) {
                this.marker.on("dragend", () => {
                    const p = this.marker.getLatLng();
                    this.setPoint(p.lat, p.lng, { manual: true });
                });
            }
        } else {
            this.marker.setLatLng([lat, lng]);
        }
        if (pan) {
            this.map.setView([lat, lng], Math.max(this.map.getZoom(), 16));
        }
    }

    syncMarker() {
        if (!this.map) {
            return;
        }
        if (this.hasPoint) {
            const cur = this.marker ? this.marker.getLatLng() : null;
            if (!cur || Math.abs(cur.lat - this.lat) > 1e-7 || Math.abs(cur.lng - this.lng) > 1e-7) {
                this.placeMarker(this.lat, this.lng);
            }
        } else if (this.marker) {
            this.marker.remove();
            this.marker = null;
        }
    }

    async setPoint(lat, lng, { manual = false } = {}) {
        if (this.readonly) {
            return;
        }
        const rlat = Math.round(lat * 1e7) / 1e7;
        const rlng = Math.round(lng * 1e7) / 1e7;
        if (manual) {
            this.pointIsAuto = false;
            this.state.autoNote = "";
        }
        await this.props.record.update({ [this.props.name]: rlat, [this.lngField]: rlng });
        this.placeMarker(rlat, rlng, false);
    }

    async clearPoint() {
        if (this.readonly) {
            return;
        }
        this.pointIsAuto = true;
        this.lastGeocoded = "";
        this.state.autoNote = "";
        await this.props.record.update({ [this.props.name]: 0, [this.lngField]: 0 });
        if (this.marker) {
            this.marker.remove();
            this.marker = null;
        }
    }

    // ------------------------------------------------------------------
    // Geocodificación
    // ------------------------------------------------------------------
    async geocode(query) {
        const url = `https://nominatim.openstreetmap.org/search?format=json&limit=6&countrycodes=mx&addressdetails=0&q=${encodeURIComponent(query)}`;
        const resp = await fetch(url, { headers: { Accept: "application/json" } });
        const rows = await resp.json();
        return (rows || []).map((r) => ({
            label: r.display_name,
            lat: parseFloat(r.lat),
            lng: parseFloat(r.lon),
        }));
    }

    scheduleAutoGeocode(delay) {
        clearTimeout(this.geocodeTimer);
        if (this.readonly || !this.pointIsAuto) {
            return;
        }
        const text = (this.addressText || "").trim();
        if (text.length < MIN_ADDRESS_LEN || text === this.lastGeocoded) {
            return;
        }
        this.geocodeTimer = setTimeout(() => this.autoGeocode(text), delay);
    }

    async autoGeocode(text) {
        if (this.readonly || !this.pointIsAuto || text !== (this.addressText || "").trim()) {
            return;
        }
        this.lastGeocoded = text;
        try {
            const results = await this.geocode(text);
            if (!this.pointIsAuto || text !== (this.addressText || "").trim()) {
                return;
            }
            if (!results.length) {
                this.state.autoNote = "No se encontró la dirección en el mapa: marca el punto o afina la dirección.";
                return;
            }
            const best = results[0];
            await this.setPoint(best.lat, best.lng);
            this.placeMarker(best.lat, best.lng, true);
            this.state.autoNote = "Ubicación estimada a partir de la dirección: verifica el punto y muévelo si no es exacto.";
        } catch (e) {
            console.error("[MAPA] geocodificación automática falló", e);
        }
    }

    // ------------------------------------------------------------------
    // Buscador en vivo
    // ------------------------------------------------------------------
    onQuery(ev) {
        this.state.query = ev.target.value;
        clearTimeout(this.liveTimer);
        const q = (this.state.query || "").trim();
        if (q.length < MIN_QUERY_LEN) {
            this.state.results = [];
            return;
        }
        this.liveTimer = setTimeout(() => this.liveSearch(q), LIVE_SEARCH_DELAY);
    }

    async liveSearch(q) {
        const seq = ++this.liveSeq;
        this.state.searching = true;
        try {
            const results = await this.geocode(q);
            // Respuesta vieja (el usuario siguió tecleando): se descarta.
            if (seq !== this.liveSeq) {
                return;
            }
            this.state.results = results;
        } catch (e) {
            console.error("[MAPA] búsqueda en vivo falló", e);
        } finally {
            if (seq === this.liveSeq) {
                this.state.searching = false;
            }
        }
    }

    onQueryKeydown(ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            const coords = parseCoords(this.state.query);
            if (coords) {
                this.useCoords(coords);
            } else if (this.state.results.length) {
                this.pick(this.state.results[0]);
            } else {
                this.search();
            }
        } else if (ev.key === "Escape") {
            this.state.results = [];
        }
    }

    async useCoords([lat, lng]) {
        this.state.results = [];
        await this.setPoint(lat, lng, { manual: true });
        this.placeMarker(lat, lng, true);
        this.state.autoNote = "Coordenadas fijadas. La dirección de entrega no se modifica.";
    }

    async search() {
        const coords = parseCoords(this.state.query);
        if (coords) {
            return this.useCoords(coords);
        }
        const q = (this.state.query || this.addressText || "").trim();
        if (!q) {
            this.notification.add("Escribe o captura primero la dirección a buscar.", { type: "warning" });
            return;
        }
        clearTimeout(this.liveTimer);
        this.liveSeq++;
        this.state.searching = true;
        this.state.results = [];
        try {
            this.state.results = await this.geocode(q);
            if (!this.state.results.length) {
                this.notification.add("Sin resultados: afina la dirección o marca el punto directo en el mapa.", { type: "warning" });
            } else if (this.state.results.length === 1) {
                await this.pick(this.state.results[0]);
            }
        } catch (e) {
            console.error("[MAPA] búsqueda de dirección falló", e);
            this.notification.add("No se pudo buscar la dirección. Marca el punto en el mapa.", { type: "danger" });
        } finally {
            this.state.searching = false;
        }
    }

    async pick(result) {
        this.state.results = [];
        this.state.query = result.label;
        // Mapa → dirección NO (27 sep 2026): el resultado del buscador solo
        // fija el punto; la dirección de entrega capturada no se toca.
        await this.setPoint(result.lat, result.lng, { manual: true });
        this.placeMarker(result.lat, result.lng, true);
        this.state.autoNote = "Punto fijado desde el buscador del mapa. La dirección de entrega no se modifica.";
    }

    locateMe() {
        if (!navigator.geolocation) {
            this.notification.add("Este navegador no comparte ubicación.", { type: "warning" });
            return;
        }
        navigator.geolocation.getCurrentPosition(
            (pos) => {
                this.setPoint(pos.coords.latitude, pos.coords.longitude, { manual: true });
                this.placeMarker(pos.coords.latitude, pos.coords.longitude, true);
            },
            () => this.notification.add("No se pudo obtener tu ubicación.", { type: "warning" }),
            { enableHighAccuracy: true, timeout: 8000 }
        );
    }

    openExternal() {
        if (this.hasPoint) {
            window.open(`https://maps.google.com/?q=${this.lat},${this.lng}`, "_blank");
        }
    }
}

export const somMapPicker = {
    component: SomMapPicker,
    displayName: "Ubicación en mapa (SOM)",
    supportedTypes: ["float"],
    extractProps: ({ options }) => ({
        lngField: options.lng_field,
        addressField: options.address_field,
    }),
};

registry.category("fields").add("som_map_picker", somMapPicker);
