/** @odoo-module **/
// SOLICITAR ENTREGA — asistente guiado desde la orden de venta (27 sep 2026).
//
// Dominio de la SOLICITUD (ventas/operación): qué materiales y cuánto,
// dónde, cuándo y especificaciones. Nada de vehículo, chofer, ticket ni
// ejecución: eso es de Logística (bandeja Entregas › Solicitudes).
//
// Pasos: 1 Materiales · 2 Dirección y mapa · 3 Fecha · 4 Especificaciones ·
// Revisar y solicitar. Cada paso valida lo suyo: el campo que falta se marca
// en rojo con su mensaje y la vista baja hasta el primero.
//
// Dirección ⇄ mapa (relación controlada):
// - Dirección → mapa: elegir, crear o editar la dirección la localiza.
// - Mapa → dirección: NUNCA. Buscar en el mapa, pegar coordenadas, mover o
//   hacer clic solo cambia latitud/longitud.
import { Component, onMounted, onPatched, onWillStart, onWillUnmount, useRef, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { parseCoords } from "@sale_delivery_wizard/components/map_picker/map_picker";

const MODEL = "sale.delivery.schedule";
const DEFAULT_CENTER = [25.6866, -100.3161]; // Monterrey
const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILES_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank">OpenStreetMap</a>';
const GEOCODE_DELAY = 900;
const SEARCH_DELAY = 450;
const MIN_ADDRESS_LEN = 10;
const PIN_SVG =
    '<svg xmlns="http://www.w3.org/2000/svg" width="30" height="42" viewBox="0 0 30 42">' +
    '<path d="M15 1C7.3 1 1 7.2 1 14.9c0 9.9 12.1 24.6 13 25.6a1.3 1.3 0 0 0 2 0c.9-1 13-15.7 13-25.6C29 7.2 22.7 1 15 1z" ' +
    'fill="#B3261E" stroke="#fff" stroke-width="2"/><circle cx="15" cy="15" r="5.5" fill="#fff"/></svg>';

const STEPS = [
    { n: 1, key: "materials", title: "¿Qué vas a entregar?", short: "Materiales" },
    { n: 2, key: "address", title: "¿Dónde se entrega?", short: "Dirección" },
    { n: 3, key: "date", title: "¿Cuándo se necesita?", short: "Fecha" },
    { n: 4, key: "specs", title: "¿Alguna especificación adicional?", short: "Especificaciones" },
    { n: 5, key: "review", title: "Revisa y solicita", short: "Revisar" },
];
// En qué paso vive cada error que puede regresar el servidor.
const FIELD_STEP = {
    lines: 1, line_errors: 1,
    partner_id: 2, contact_name: 2, contact_phone: 2, delivery_address: 2, location: 2,
    date: 3, time_window: 3, time_exact: 3,
};
const MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];

function fmtQty(q) {
    if (q === null || q === undefined) {
        return "—";
    }
    return (Math.round(q * 100) / 100).toLocaleString("es-MX", { maximumFractionDigits: 2 });
}

function fmtIso(iso) {
    if (!iso) {
        return "";
    }
    const [y, m, d] = iso.split("-").map((x) => parseInt(x, 10));
    return `${d} ${MESES[m - 1]} ${y}`;
}

export class DeliveryRequest extends Component {
    static template = "sale_delivery_wizard.DeliveryRequest";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.mapRef = useRef("map");
        this.rootRef = useRef("root");
        const params = (this.props.action && this.props.action.params) || {};
        const ctx = (this.props.action && this.props.action.context) || {};
        this.orderId = params.order_id || ctx.som_request_order_id || ctx.active_id || false;
        this.steps = STEPS;
        this.state = useState({
            loading: true,
            loadError: "",
            data: null,
            step: 1,
            maxStep: 1,
            errors: {},
            lineErrors: {},
            selected: {}, // sale_line_id → bool
            qty: {}, // sale_line_id → número
            addressId: false,
            address: { partner_id: false, contact_name: "", contact_phone: "", delivery_address: "", latitude: 0, longitude: 0 },
            creating: false,
            newAddr: this.emptyNewAddress(),
            newAddrErrors: {},
            savingAddr: false,
            date: "",
            timeWindow: "any",
            timeExact: "",
            instructions: "",
            submitting: false,
            done: null,
            // mapa
            mapQuery: "",
            mapResults: [],
            mapSearching: false,
            mapNote: "",
            geocoding: false,
        });
        this.map = null;
        this.marker = null;
        this.geocodeTimer = null;
        this.searchTimer = null;
        this.searchSeq = 0;
        this.geocodeSeq = 0;
        this.scrollToError = false;

        onWillStart(() => this.load());
        onMounted(() => this.afterRender());
        onPatched(() => this.afterRender());
        onWillUnmount(() => {
            clearTimeout(this.geocodeTimer);
            clearTimeout(this.searchTimer);
            if (this.resizeObserver) {
                this.resizeObserver.disconnect();
            }
            if (this.map) {
                this.map.remove();
                this.map = null;
            }
        });
    }

    emptyNewAddress() {
        return { name: "", phone: "", street: "", number: "", street2: "", city: "", state_id: "", zip: "", references: "" };
    }

    // ------------------------------------------------------------------
    // Carga
    // ------------------------------------------------------------------
    async load() {
        if (!this.orderId) {
            this.state.loadError = "Abre esta pantalla desde una orden de venta (botón «Solicitar entrega»).";
            this.state.loading = false;
            return;
        }
        try {
            const data = await this.orm.call(MODEL, "request_prepare", [this.orderId]);
            this.state.data = data;
            this.state.date = data.default_date;
            const preferred = data.addresses.find((a) => a.id === data.default_address_id);
            if (preferred) {
                this.applyAddress(preferred, { geocode: false });
            }
        } catch (e) {
            this.state.loadError = (e && e.data && e.data.message) || "No se pudo cargar la orden.";
        } finally {
            this.state.loading = false;
        }
    }

    get data() {
        return this.state.data || { lines: [], addresses: [], time_windows: [], states: [], existing: [], order: {} };
    }

    get currentStep() {
        return STEPS[this.state.step - 1];
    }

    // ------------------------------------------------------------------
    // Paso 1 — materiales
    // ------------------------------------------------------------------
    fmtQty(q) {
        return fmtQty(q);
    }

    isSelectable(line) {
        return line.pending > 0.0001;
    }

    toggleLine(line) {
        if (!this.isSelectable(line)) {
            return;
        }
        const on = !this.state.selected[line.sale_line_id];
        this.state.selected[line.sale_line_id] = on;
        if (on && !(this.state.qty[line.sale_line_id] > 0)) {
            this.state.qty[line.sale_line_id] = Math.round(line.pending * 100) / 100;
        }
        delete this.state.lineErrors[line.sale_line_id];
        delete this.state.errors.lines;
    }

    onQty(line, ev) {
        const raw = String(ev.target.value || "").replace(",", ".");
        const val = parseFloat(raw);
        this.state.qty[line.sale_line_id] = isNaN(val) ? "" : val;
        this.state.selected[line.sale_line_id] = !isNaN(val) && val > 0;
        delete this.state.lineErrors[line.sale_line_id];
        delete this.state.errors.lines;
    }

    fillPending(line) {
        if (!this.isSelectable(line)) {
            return;
        }
        this.state.qty[line.sale_line_id] = Math.round(line.pending * 100) / 100;
        this.state.selected[line.sale_line_id] = true;
        delete this.state.lineErrors[line.sale_line_id];
        delete this.state.errors.lines;
    }

    qtyValue(line) {
        const q = this.state.qty[line.sale_line_id];
        return q === undefined || q === null ? "" : q;
    }

    onRowClick(line, ev) {
        const tag = ev.target.tagName;
        if (tag !== "INPUT" && tag !== "BUTTON") {
            this.toggleLine(line);
        }
    }

    get chosenLines() {
        return this.data.lines
            .filter((l) => this.state.selected[l.sale_line_id] && this.state.qty[l.sale_line_id] > 0)
            .map((l) => ({ ...l, qty: this.state.qty[l.sale_line_id] }));
    }

    availabilityWarn(line) {
        const q = this.state.qty[line.sale_line_id];
        return this.state.selected[line.sale_line_id] && line.available !== null && q > line.available + 0.0001;
    }

    // ------------------------------------------------------------------
    // Paso 2 — dirección y mapa
    // ------------------------------------------------------------------
    selectAddress(addr) {
        this.state.creating = false;
        this.applyAddress(addr, { geocode: true });
    }

    applyAddress(addr, { geocode }) {
        this.state.addressId = addr.id;
        const hasPoint = Boolean(addr.latitude && addr.longitude);
        this.state.address = {
            partner_id: addr.id,
            contact_name: addr.contact_name || addr.name || "",
            contact_phone: addr.contact_phone || "",
            delivery_address: addr.address || "",
            latitude: addr.latitude || 0,
            longitude: addr.longitude || 0,
        };
        for (const k of ["partner_id", "contact_name", "contact_phone", "delivery_address", "location"]) {
            delete this.state.errors[k];
        }
        this.state.mapNote = "";
        // El punto anterior (de otra dirección) no se queda en el mapa.
        this.syncMarker(hasPoint);
        if (hasPoint) {
            this.state.mapNote = "Ubicación guardada de esta dirección. Verifica que corresponda al lugar esperado.";
        } else if (geocode || this.state.step === 2) {
            // Dirección → mapa: se intenta localizar.
            this.scheduleGeocode(0);
        } else {
            this.pendingGeocode = true;
        }
    }

    onAddressField(key, ev) {
        this.state.address[key] = ev.target.value;
        delete this.state.errors[key];
        if (key === "delivery_address") {
            // Editar la dirección la vuelve a localizar en el mapa.
            this.scheduleGeocode(GEOCODE_DELAY);
        }
    }

    startCreate() {
        this.state.creating = true;
        this.state.newAddr = this.emptyNewAddress();
        this.state.newAddr.phone = this.state.address.contact_phone || "";
        this.state.newAddrErrors = {};
        this.scrollToError = false;
        this.focusAfterRender = ".o_dr_newaddr input";
    }

    cancelCreate() {
        this.state.creating = false;
        this.state.newAddrErrors = {};
    }

    onNewAddr(key, ev) {
        this.state.newAddr[key] = ev.target.value;
        delete this.state.newAddrErrors[key];
    }

    isStateSelected(st) {
        return String(this.state.newAddr.state_id) === String(st.id);
    }

    async saveNewAddress() {
        const errs = {};
        const req = ["name", "phone", "street", "number", "street2", "city", "state_id", "zip"];
        for (const k of req) {
            if (!String(this.state.newAddr[k] || "").trim()) {
                errs[k] = "Este campo es obligatorio.";
            }
        }
        if (Object.keys(errs).length) {
            this.state.newAddrErrors = errs;
            this.scrollToError = true;
            return;
        }
        this.state.savingAddr = true;
        try {
            const res = await this.orm.call(MODEL, "request_create_address", [this.orderId, this.state.newAddr]);
            if (res.errors) {
                this.state.newAddrErrors = res.errors;
                this.scrollToError = true;
                return;
            }
            this.data.addresses.unshift(res.address);
            this.state.creating = false;
            this.applyAddress(res.address, { geocode: true });
            this.notification.add("Dirección de entrega creada y seleccionada.", { type: "success" });
        } catch (e) {
            this.notification.add((e && e.data && e.data.message) || "No se pudo crear la dirección.", { type: "danger" });
        } finally {
            this.state.savingAddr = false;
        }
    }

    // --- mapa ---
    afterRender() {
        if (this.state.step === 2 && this.mapRef.el && window.L) {
            if (!this.map) {
                this.initMap();
            } else {
                this.map.invalidateSize();
            }
            if (this.pendingGeocode) {
                this.pendingGeocode = false;
                this.scheduleGeocode(0);
            }
        }
        if (this.scrollToError && this.rootRef.el) {
            this.scrollToError = false;
            const el = this.rootRef.el.querySelector(".o_dr_invalid, .o_dr_err");
            if (el) {
                el.scrollIntoView({ behavior: "smooth", block: "center" });
                const input = el.matches("input, textarea, select") ? el : el.querySelector("input, textarea, select");
                if (input) {
                    input.focus({ preventScroll: true });
                }
            }
        }
        if (this.focusAfterRender && this.rootRef.el) {
            const el = this.rootRef.el.querySelector(this.focusAfterRender);
            this.focusAfterRender = null;
            if (el) {
                el.focus();
            }
        }
    }

    initMap() {
        const L = window.L;
        const a = this.state.address;
        const hasPoint = Boolean(a.latitude && a.longitude);
        this.map = L.map(this.mapRef.el, {
            center: hasPoint ? [a.latitude, a.longitude] : DEFAULT_CENTER,
            zoom: hasPoint ? 16 : 11,
        });
        L.tileLayer(TILES, { maxZoom: 19, attribution: TILES_ATTR }).addTo(this.map);
        // Mapa → coordenadas (nunca la dirección).
        this.map.on("click", (ev) => this.setPoint(ev.latlng.lat, ev.latlng.lng, "Punto marcado en el mapa."));
        if (window.ResizeObserver) {
            this.resizeObserver = new ResizeObserver(() => this.map && this.map.invalidateSize());
            this.resizeObserver.observe(this.mapRef.el);
        }
        this.syncMarker(true);
    }

    pinIcon() {
        return window.L.divIcon({ className: "o_dr_pin", html: PIN_SVG, iconSize: [30, 42], iconAnchor: [15, 41] });
    }

    syncMarker(pan) {
        if (!this.map) {
            return;
        }
        const a = this.state.address;
        if (a.latitude && a.longitude) {
            if (!this.marker) {
                this.marker = window.L.marker([a.latitude, a.longitude], { draggable: true, icon: this.pinIcon() }).addTo(this.map);
                this.marker.on("dragend", () => {
                    const p = this.marker.getLatLng();
                    this.setPoint(p.lat, p.lng, "Punto ajustado a mano.");
                });
            } else {
                this.marker.setLatLng([a.latitude, a.longitude]);
            }
            if (pan) {
                this.map.setView([a.latitude, a.longitude], Math.max(this.map.getZoom(), 16));
            }
        } else if (this.marker) {
            this.marker.remove();
            this.marker = null;
        }
    }

    setPoint(lat, lng, note) {
        this.state.address.latitude = Math.round(lat * 1e7) / 1e7;
        this.state.address.longitude = Math.round(lng * 1e7) / 1e7;
        delete this.state.errors.location;
        if (note) {
            this.state.mapNote = note + " La dirección de entrega no se modifica.";
        }
        this.syncMarker(false);
    }

    async geocode(query) {
        const url = `https://nominatim.openstreetmap.org/search?format=json&limit=6&countrycodes=mx&addressdetails=0&q=${encodeURIComponent(query)}`;
        const resp = await fetch(url, { headers: { Accept: "application/json" } });
        const rows = await resp.json();
        return (rows || []).map((r) => ({ label: r.display_name, lat: parseFloat(r.lat), lng: parseFloat(r.lon) }));
    }

    scheduleGeocode(delay) {
        clearTimeout(this.geocodeTimer);
        const text = (this.state.address.delivery_address || "").replace(/\s+/g, " ").trim();
        if (text.length < MIN_ADDRESS_LEN) {
            return;
        }
        this.geocodeTimer = setTimeout(() => this.autoGeocode(text), delay);
    }

    async autoGeocode(text) {
        const seq = ++this.geocodeSeq;
        this.state.geocoding = true;
        try {
            const results = await this.geocode(text);
            if (seq !== this.geocodeSeq) {
                return;
            }
            if (!results.length) {
                this.state.mapNote = "No se encontró la dirección en el mapa: búscala arriba, pega coordenadas o haz clic en el punto de entrega.";
                return;
            }
            const best = results[0];
            this.setPoint(best.lat, best.lng);
            this.syncMarker(true);
            this.state.mapNote = "Ubicación encontrada a partir de la dirección: verifica que sea el lugar correcto y mueve el punto si hace falta.";
        } catch (e) {
            console.error("[SOLICITUD] geocodificación falló", e);
        } finally {
            if (seq === this.geocodeSeq) {
                this.state.geocoding = false;
            }
        }
    }

    onMapQuery(ev) {
        this.state.mapQuery = ev.target.value;
        clearTimeout(this.searchTimer);
        const q = this.state.mapQuery.trim();
        if (q.length < 4 || parseCoords(q)) {
            this.state.mapResults = [];
            return;
        }
        this.searchTimer = setTimeout(() => this.mapSearch(q), SEARCH_DELAY);
    }

    async mapSearch(q) {
        const seq = ++this.searchSeq;
        this.state.mapSearching = true;
        try {
            const results = await this.geocode(q);
            if (seq === this.searchSeq) {
                this.state.mapResults = results;
            }
        } catch (e) {
            console.error("[SOLICITUD] búsqueda en mapa falló", e);
        } finally {
            if (seq === this.searchSeq) {
                this.state.mapSearching = false;
            }
        }
    }

    onMapQueryKey(ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            const coords = parseCoords(this.state.mapQuery);
            if (coords) {
                this.setPoint(coords[0], coords[1], "Coordenadas fijadas.");
                this.syncMarker(true);
            } else if (this.state.mapResults.length) {
                this.pickResult(this.state.mapResults[0]);
            }
        } else if (ev.key === "Escape") {
            this.state.mapResults = [];
        }
    }

    pickResult(r) {
        this.state.mapResults = [];
        // Mapa → dirección NO: solo el punto.
        this.setPoint(r.lat, r.lng, "Punto fijado desde el buscador del mapa.");
        this.syncMarker(true);
    }

    relocate() {
        this.scheduleGeocode(0);
    }

    // ------------------------------------------------------------------
    // Paso 3 — fecha
    // ------------------------------------------------------------------
    setWindow(key) {
        this.state.timeWindow = key;
        delete this.state.errors.time_exact;
    }

    onDate(ev) {
        this.state.date = ev.target.value;
        delete this.state.errors.date;
    }

    onTime(ev) {
        this.state.timeExact = ev.target.value;
        delete this.state.errors.time_exact;
    }

    get timeExactFloat() {
        const [h, m] = String(this.state.timeExact || "").split(":").map((x) => parseInt(x, 10));
        if (isNaN(h)) {
            return 0;
        }
        return h + (isNaN(m) ? 0 : m / 60);
    }

    get windowLabel() {
        if (this.state.timeWindow === "exact") {
            return this.state.timeExact ? `${this.state.timeExact} h` : "Hora exacta";
        }
        const w = this.data.time_windows.find((t) => t.key === this.state.timeWindow);
        return w ? w.label : "";
    }

    fmtIso(iso) {
        return fmtIso(iso);
    }

    onInstructions(ev) {
        this.state.instructions = ev.target.value;
    }

    // ------------------------------------------------------------------
    // Validación y navegación
    // ------------------------------------------------------------------
    validateStep(n) {
        const errors = {};
        const lineErrors = {};
        if (n === 1) {
            const chosen = this.chosenLines;
            for (const l of this.data.lines) {
                if (!this.state.selected[l.sale_line_id]) {
                    continue;
                }
                const q = this.state.qty[l.sale_line_id];
                if (!(q > 0)) {
                    lineErrors[l.sale_line_id] = "Captura la cantidad.";
                } else if (q > l.pending + 0.0001) {
                    lineErrors[l.sale_line_id] = `Máximo ${fmtQty(l.pending)} ${l.uom} pendientes.`;
                }
            }
            if (Object.keys(lineErrors).length) {
                errors.lines = "Revisa las cantidades marcadas.";
            } else if (!chosen.length) {
                errors.lines = "Selecciona al menos un material y la cantidad a entregar.";
            }
        } else if (n === 2) {
            const a = this.state.address;
            if (!this.state.addressId && !a.delivery_address) {
                errors.partner_id = "Elige una dirección del cliente o crea una nueva.";
            }
            if (!String(a.contact_name || "").trim()) {
                errors.contact_name = "Este campo es obligatorio.";
            }
            if (!String(a.contact_phone || "").trim()) {
                errors.contact_phone = "Este campo es obligatorio.";
            }
            if (String(a.delivery_address || "").trim().length < MIN_ADDRESS_LEN) {
                errors.delivery_address = "Este campo es obligatorio: calle, número, colonia y ciudad.";
            }
            if (!(a.latitude && a.longitude)) {
                errors.location = "Ubica la dirección en el mapa: búscala o haz clic en el punto de entrega.";
            }
        } else if (n === 3) {
            if (!this.state.date) {
                errors.date = "Este campo es obligatorio.";
            } else if (this.state.date < this.data.today) {
                errors.date = "La fecha no puede ser anterior a hoy.";
            }
            if (this.state.timeWindow === "exact" && !this.state.timeExact) {
                errors.time_exact = "Captura la hora exacta.";
            }
        }
        return { errors, lineErrors };
    }

    showErrors(errors, lineErrors) {
        this.state.errors = errors;
        this.state.lineErrors = lineErrors || {};
        this.scrollToError = true;
    }

    next() {
        const { errors, lineErrors } = this.validateStep(this.state.step);
        if (Object.keys(errors).length) {
            this.showErrors(errors, lineErrors);
            return;
        }
        this.state.errors = {};
        this.state.lineErrors = {};
        this.goTo(this.state.step + 1);
    }

    back() {
        this.goTo(this.state.step - 1);
    }

    goTo(n) {
        if (n < 1 || n > STEPS.length) {
            return;
        }
        // Hacia adelante solo si los pasos intermedios están completos.
        for (let i = this.state.step; i < n; i++) {
            const { errors, lineErrors } = this.validateStep(i);
            if (Object.keys(errors).length) {
                this.state.step = i;
                this.showErrors(errors, lineErrors);
                return;
            }
        }
        this.state.errors = {};
        this.state.lineErrors = {};
        this.state.step = n;
        this.state.maxStep = Math.max(this.state.maxStep, n);
        if (this.rootRef.el) {
            this.rootRef.el.scrollTop = 0;
        }
    }

    clickStep(s) {
        if (s.n <= this.state.maxStep) {
            this.goTo(s.n);
        }
    }

    stepClass(s) {
        return {
            on: this.state.step === s.n,
            done: s.n < this.state.step,
            reachable: s.n <= this.state.maxStep,
        };
    }

    async submit() {
        for (let i = 1; i <= 4; i++) {
            const { errors, lineErrors } = this.validateStep(i);
            if (Object.keys(errors).length) {
                this.state.step = i;
                this.showErrors(errors, lineErrors);
                return;
            }
        }
        this.state.submitting = true;
        try {
            const payload = {
                lines: this.chosenLines.map((l) => ({ sale_line_id: l.sale_line_id, qty: l.qty })),
                address: { ...this.state.address },
                date: this.state.date,
                time_window: this.state.timeWindow,
                time_exact: this.state.timeWindow === "exact" ? this.timeExactFloat : 0,
                instructions: this.state.instructions,
            };
            const res = await this.orm.call(MODEL, "request_submit", [this.orderId, payload]);
            if (res.errors) {
                if (res.errors.general) {
                    this.notification.add(res.errors.general, { type: "danger", sticky: true });
                }
                const lineErrors = {};
                for (const [k, v] of Object.entries(res.errors.line_errors || {})) {
                    lineErrors[parseInt(k, 10)] = v;
                }
                const fields = Object.keys(res.errors).filter((k) => FIELD_STEP[k]);
                if (fields.length) {
                    this.state.step = Math.min(...fields.map((k) => FIELD_STEP[k]));
                    this.showErrors(res.errors, lineErrors);
                }
                return;
            }
            this.state.done = res;
        } catch (e) {
            this.notification.add((e && e.data && e.data.message) || "No se pudo enviar la solicitud.", { type: "danger", sticky: true });
        } finally {
            this.state.submitting = false;
        }
    }

    // ------------------------------------------------------------------
    // Salidas
    // ------------------------------------------------------------------
    openOrder() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "sale.order",
            res_id: this.orderId,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openRequest() {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: MODEL,
            res_id: this.state.done.id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    async another() {
        this.state.done = null;
        this.state.step = 1;
        this.state.maxStep = 1;
        this.state.selected = {};
        this.state.qty = {};
        this.state.instructions = "";
        this.state.loading = true;
        await this.load();
    }
}

registry.category("actions").add("sale_delivery_wizard.delivery_request", DeliveryRequest);
