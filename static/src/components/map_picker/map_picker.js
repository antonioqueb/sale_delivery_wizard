/** @odoo-module **/
// Selector de ubicación en mapa (Leaflet vendorizado del módulo).
//
// Widget de campo para la LATITUD; escribe también la LONGITUD (option
// lng_field). Clic o arrastre del marcador fija el punto; "Buscar
// dirección" geocodifica el texto del campo de dirección (option
// address_field) con Nominatim; "Mi ubicación" usa el GPS del navegador.
//
// 21 sep 2026 — mapa "a priori":
// - El mapa se pinta aunque el contenedor nazca sin tamaño (registro nuevo
//   que se abre desde «Programar entrega»): se reintenta la inicialización
//   en cada patch y un ResizeObserver redibuja en cuanto el contenedor mide.
// - Sin punto pero con dirección, el widget geocodifica solo al abrir y
//   cada vez que cambia el texto de la dirección (mientras el vendedor no
//   haya fijado el punto a mano). Así el vendedor ve la ubicación estimada
//   sin guardar y solo la corrige si hace falta.
import { Component, onMounted, onPatched, onWillUnmount, useEffect, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

const DEFAULT_CENTER = [25.6866, -100.3161]; // Monterrey
const TILES = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const AUTO_GEOCODE_DELAY = 900; // ms sin teclear antes de geocodificar la dirección
const MIN_ADDRESS_LEN = 10;

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
        // vendedor fija el punto a mano (clic, arrastre, búsqueda, GPS).
        this.pointIsAuto = !this.hasPoint;
        this.lastGeocoded = "";
        this.geocodeTimer = null;

        onMounted(() => {
            this.ensureMap();
            this.scheduleAutoGeocode(0);
        });
        // Registro nuevo: el form puede pintar el contenedor después del
        // primer mount (o sin tamaño). Se reintenta hasta que exista.
        onPatched(() => this.ensureMap());
        onWillUnmount(() => {
            clearTimeout(this.geocodeTimer);
            if (this.resizeObserver) {
                this.resizeObserver.disconnect();
                this.resizeObserver = null;
            }
            if (this.map) {
                this.map.remove();
                this.map = null;
            }
        });
        // Si el registro cambia de coordenadas por fuera (p. ej. valores por
        // defecto al abrir o al elegir otra dirección del cliente), el
        // marcador sigue al dato.
        useEffect(
            () => this.syncMarker(),
            () => [this.lat, this.lng]
        );
        // La dirección cambió (a mano o por el selector de dirección del
        // cliente): si el punto no fue fijado a mano, se vuelve a estimar.
        useEffect(
            () => this.scheduleAutoGeocode(AUTO_GEOCODE_DELAY),
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

    initMap() {
        const L = window.L;
        if (!L || !this.mapRef.el) {
            return;
        }
        const center = this.hasPoint ? [this.lat, this.lng] : DEFAULT_CENTER;
        this.map = L.map(this.mapRef.el, { center, zoom: this.hasPoint ? 16 : 11, zoomControl: true });
        L.tileLayer(TILES, { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(this.map);
        if (this.hasPoint) {
            this.placeMarker(this.lat, this.lng, false);
        }
        if (!this.readonly) {
            this.map.on("click", (ev) => this.setPoint(ev.latlng.lat, ev.latlng.lng, { manual: true }));
        }
        // El contenedor puede medir 0 al crear el mapa (form que se pinta
        // después, grupo animado, registro nuevo): Leaflet no carga teselas
        // hasta que se le avisa del tamaño real. El ResizeObserver lo hace
        // en cuanto el contenedor mide algo y en cada cambio de tamaño.
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
            this.marker = L.marker([lat, lng], { draggable: !this.readonly }).addTo(this.map);
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
        // Quitar el punto vuelve a dejar el mapa en manos de la dirección.
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
        const url = `https://nominatim.openstreetmap.org/search?format=json&limit=5&countrycodes=mx&q=${encodeURIComponent(query)}`;
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
            // La dirección pudo cambiar mientras se buscaba: se descarta.
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

    onQuery(ev) {
        this.state.query = ev.target.value;
    }

    onQueryKeydown(ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            this.search();
        }
    }

    async search() {
        const q = (this.state.query || this.addressText || "").trim();
        if (!q) {
            this.notification.add("Escribe o captura primero la dirección a buscar.", { type: "warning" });
            return;
        }
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
        await this.setPoint(result.lat, result.lng, { manual: true });
        this.placeMarker(result.lat, result.lng, true);
        this.state.results = [];
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
