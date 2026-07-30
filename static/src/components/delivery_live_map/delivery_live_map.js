/** @odoo-module **/
// Mapa de entregas (client action OWL + Leaflet vendorizado).
//
// - Siempre pinta el mapa (Monterrey, MX) aunque no haya choferes activos.
// - En vivo: TODOS los viajes del día, polling cada 15 s; Rutas: histórico.
// - Rutas súper marcadas (casing blanco + color) sobre CANVAS (preferCanvas):
//   el render SVG punto-por-punto era lo que hacía lento el zoom.
// - Clic en la ruta → distancia, tiempos y velocidades del viaje.
// - Clic en el camioncito/bandera → PT, orden de venta, cliente, material y
//   cantidades entregadas, métricas completas y liga a Google Maps.
// - Panel lateral de accesos directos: un clic y el mapa vuela a ese viaje.
// - Si los datos no cambiaron entre polls NO se redibuja (los popups no se
//   cierran solos y el mapa no "parpadea").
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onMounted, onWillUnmount, useRef, useState } from "@odoo/owl";

const EVENT_ICONS = {
    inicio: "\u{1F7E2}",
    llegada: "\u{1F4CD}",
    firma: "✍️",
    fin: "\u{1F3C1}",
};
const EVENT_LABELS = {
    inicio: "Inicio de ruta",
    llegada: "Llegada",
    firma: "Firma",
    fin: "Fin de ruta",
};

function esc(s) {
    return String(s == null ? "" : s).replace(
        /[&<>"']/g,
        (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
    );
}

function fmtDur(min) {
    min = Math.max(0, Math.round(min || 0));
    const h = Math.floor(min / 60);
    return h ? `${h} h ${min % 60} min` : `${min} min`;
}

export class DeliveryLiveMap extends Component {
    static template = "sale_delivery_wizard.DeliveryLiveMap";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.mapRef = useRef("map");
        const action = this.props.action || {};
        this.mode =
            (action.params && action.params.mode) ||
            (action.context && action.context.map_mode) ||
            "live";
        this.state = useState({
            routes: [], // resumen para el panel de accesos directos
            title: this.mode === "live" ? "Viajes de hoy" : "Rutas",
        });
        this.timer = null;
        this.fitted = false;
        this.lastPayload = null;
        this.routeLayers = {}; // id -> {group, bounds}
        onMounted(() => this.start());
        onWillUnmount(() => this.stop());
    }

    start() {
        const L = window.L;
        if (!L || !this.mapRef.el) {
            return;
        }
        this.map = L.map(this.mapRef.el, {
            scrollWheelZoom: true,
            preferCanvas: true, // polylines en canvas: zoom fluido
            zoomAnimation: true,
            wheelDebounceTime: 25,
        }).setView([25.6866, -100.3161], 12); // Monterrey, MX
        L.tileLayer(
            "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
            { attribution: "&copy; OpenStreetMap &copy; CARTO", maxZoom: 19, updateWhenZooming: false }
        ).addTo(this.map);
        this.layer = L.layerGroup().addTo(this.map);
        this.load();
        this.timer = setInterval(
            () => this.load(),
            this.mode === "live" ? 15000 : 60000
        );
    }

    stop() {
        if (this.timer) {
            clearInterval(this.timer);
            this.timer = null;
        }
        if (this.map) {
            this.map.remove();
            this.map = null;
        }
    }

    async load() {
        if (!this.map) {
            return;
        }
        let data;
        try {
            data = await this.orm.call("sale.delivery.live.map", "get_route_data", [this.mode]);
        } catch {
            return; // red caída o sesión expirada: el mapa se queda como está
        }
        if (!this.map || !data) {
            return;
        }
        const payload = JSON.stringify(data);
        if (payload === this.lastPayload) {
            return; // nada cambió: no parpadear ni cerrar popups
        }
        this.lastPayload = payload;
        this.draw(data);
    }

    // ── Popups ─────────────────────────────────────────────────────────

    metricsHtml(r) {
        return (
            `<div class="o_dlm_metrics">` +
            `<span>📏 <b>${r.distance_km}</b> km</span>` +
            `<span>⏱ ${esc(fmtDur(r.duration_min))}</span>` +
            `<span>⌀ <b>${r.avg_kmh}</b> km/h</span>` +
            `<span>🚀 pico <b>${r.peak_kmh}</b> km/h</span>` +
            `<span>⏸ detenido ${esc(fmtDur(r.stopped_min))}</span>` +
            `</div>`
        );
    }

    routePopup(r) {
        return (
            `<div class="o_dlm_popup">` +
            `<div class="o_dlm_pop_title">${r.finished ? "🏁" : "🚚"} ${esc(r.label)}</div>` +
            this.metricsHtml(r) +
            `<div class="o_dlm_pop_muted">Inició ${esc(r.start_time)}</div>` +
            `</div>`
        );
    }

    truckPopup(r) {
        const mats = (r.materials || [])
            .map(
                (m) =>
                    `<div class="o_dlm_mat"><span>${esc(m.product)}</span><b>${m.qty}</b></div>`
            )
            .join("");
        return (
            `<div class="o_dlm_popup">` +
            `<div class="o_dlm_pop_title">${r.finished ? "🏁" : "🚚"} ${esc(r.doc_name)}</div>` +
            `<div class="o_dlm_pop_rows">` +
            (r.pt_folio ? `<div><span>PT</span><b>${esc(r.pt_folio)}</b></div>` : "") +
            (r.sale_order ? `<div><span>Orden de venta</span><b>${esc(r.sale_order)}</b></div>` : "") +
            (r.partner ? `<div><span>Cliente</span><b>${esc(r.partner)}</b></div>` : "") +
            (r.driver ? `<div><span>Chofer</span><b>${esc(r.driver)}</b></div>` : "") +
            (r.vehicle ? `<div><span>Unidad</span><b>${esc(r.vehicle)}</b></div>` : "") +
            `</div>` +
            (mats
                ? `<div class="o_dlm_pop_sec">Material entregado</div>${mats}`
                : "") +
            this.metricsHtml(r) +
            `<div class="o_dlm_pop_muted">Último reporte: ${esc(r.last.time)} · ` +
            `<a href="https://maps.google.com/?q=${r.last.lat},${r.last.lng}" target="_blank">Google Maps</a></div>` +
            `</div>`
        );
    }

    // ── Dibujo ─────────────────────────────────────────────────────────

    draw(data) {
        const L = window.L;
        this.layer.clearLayers();
        this.routeLayers = {};
        const allBounds = [];
        const summaries = [];

        for (const r of data.routes || []) {
            const group = L.featureGroup();
            allBounds.push(...r.latlngs);

            // Ruta SÚPER marcada: casing blanco + trazo grueso de color.
            L.polyline(r.latlngs, {
                color: "#ffffff",
                weight: 9,
                opacity: 0.9,
            }).addTo(group);
            const line = L.polyline(r.latlngs, {
                color: r.color,
                weight: 5,
                opacity: 0.95,
            }).addTo(group);
            line.bindPopup(this.routePopup(r), { maxWidth: 320 });

            // Camioncito (o bandera al terminar) con TODA la información.
            const emoji = r.finished ? "\u{1F3C1}" : "\u{1F69A}";
            L.marker([r.last.lat, r.last.lng], {
                icon: L.divIcon({
                    html: `<div class="o_dlm_truck">${emoji}</div>`,
                    className: "",
                    iconSize: [30, 30],
                    iconAnchor: [15, 15],
                }),
                zIndexOffset: 1000,
            })
                .addTo(group)
                .bindPopup(this.truckPopup(r), { maxWidth: 340 });

            for (const ev of r.events || []) {
                if (this.mode === "live" && ev.type === "inicio") {
                    continue; // en vivo, el inicio lo marca la propia ruta
                }
                if (ev.type === "fin") {
                    continue; // el fin ya es la bandera del camioncito
                }
                L.marker([ev.lat, ev.lng], {
                    icon: L.divIcon({
                        html: `<div class="o_dlm_event">${EVENT_ICONS[ev.type] || "•"}</div>`,
                        className: "",
                        iconSize: [20, 20],
                        iconAnchor: [10, 10],
                    }),
                })
                    .addTo(group)
                    .bindPopup(
                        `<div class="o_dlm_popup"><div class="o_dlm_pop_title">` +
                            `${EVENT_ICONS[ev.type] || ""} ${esc(EVENT_LABELS[ev.type] || ev.type)}</div>` +
                            `<div class="o_dlm_pop_rows">` +
                            `<div><span>Hora</span><b>${esc(ev.time)}</b></div>` +
                            (r.doc_name ? `<div><span>Remisión</span><b>${esc(r.doc_name)}</b></div>` : "") +
                            (r.sale_order ? `<div><span>OV</span><b>${esc(r.sale_order)}</b></div>` : "") +
                            `</div></div>`,
                        { maxWidth: 280 }
                    );
            }

            group.addTo(this.layer);
            this.routeLayers[r.id] = group;

            summaries.push({
                id: r.id,
                label: r.label,
                color: r.color,
                km: r.distance_km,
                status: r.finished ? "Terminado" : "En ruta",
                finished: r.finished,
            });
        }

        this.state.routes = summaries;

        if (!this.fitted && allBounds.length) {
            this.map.fitBounds(allBounds, { padding: [40, 40], maxZoom: 13 });
            this.fitted = true;
        }
    }

    // ── Accesos directos ───────────────────────────────────────────────

    focusRoute(id) {
        const entry = this.routeLayers[id];
        if (entry && this.map) {
            const b = entry.getBounds();
            if (b.isValid()) {
                this.map.fitBounds(b, { padding: [60, 60], maxZoom: 15 });
            }
        }
    }

    fitAll() {
        if (!this.map) {
            return;
        }
        const groups = Object.values(this.routeLayers);
        if (!groups.length) {
            this.map.setView([25.6866, -100.3161], 12);
            return;
        }
        let bounds = null;
        for (const g of groups) {
            const b = g.getBounds();
            if (b.isValid()) {
                bounds = bounds ? bounds.extend(b) : window.L.latLngBounds(b.getSouthWest(), b.getNorthEast());
            }
        }
        if (bounds) {
            this.map.fitBounds(bounds, { padding: [40, 40], maxZoom: 13 });
        }
    }
}

registry.category("actions").add("sale_delivery_wizard.live_map", DeliveryLiveMap);
