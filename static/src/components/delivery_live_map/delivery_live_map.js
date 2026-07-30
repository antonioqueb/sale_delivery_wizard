/** @odoo-module **/
// Mapa de entregas (client action OWL + Leaflet vendorizado).
// - Siempre pinta el mapa, haya o no choferes activos (Monterrey por defecto).
// - En vivo: polling cada 15 s sin recargar; Rutas (histórico): cada 60 s.
// El HTML embebido en un campo html NO ejecuta <script> (innerHTML), por eso
// el enfoque anterior mostraba el mapa en blanco — este componente es nativo.
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onMounted, onWillUnmount, useRef } from "@odoo/owl";

const EVENT_ICONS = {
    inicio: "\u{1F7E2}",
    llegada: "\u{1F4CD}",
    firma: "✍️",
    fin: "\u{1F3C1}",
};

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
        this.timer = null;
        this.fitted = false;
        onMounted(() => this.start());
        onWillUnmount(() => this.stop());
    }

    start() {
        const L = window.L;
        if (!L || !this.mapRef.el) {
            return;
        }
        // Monterrey, MX — el mapa vive aquí aunque no haya ni un punto GPS.
        this.map = L.map(this.mapRef.el, { scrollWheelZoom: true }).setView(
            [25.6866, -100.3161],
            12
        );
        L.tileLayer(
            "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png",
            { attribution: "&copy; OpenStreetMap &copy; CARTO", maxZoom: 19 }
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
            data = await this.orm.call(
                "sale.delivery.live.map",
                "get_route_data",
                [this.mode]
            );
        } catch {
            return; // red caída o sesión expirada: el mapa se queda como está
        }
        if (this.map && data) {
            this.draw(data);
        }
    }

    draw(data) {
        const L = window.L;
        this.layer.clearLayers();
        const bounds = [];
        const esc = (s) =>
            String(s == null ? "" : s).replace(
                /[&<>"']/g,
                (c) =>
                    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
            );
        for (const r of data.routes || []) {
            bounds.push(...r.latlngs);
            L.polyline(r.latlngs, {
                color: r.color,
                weight: 4,
                opacity: 0.75,
            }).addTo(this.layer);
            const emoji = r.finished ? "\u{1F3C1}" : "\u{1F69A}";
            L.marker([r.last.lat, r.last.lng], {
                icon: L.divIcon({
                    html: `<div style="font-size:26px;filter:drop-shadow(0 1px 2px rgba(0,0,0,.4))">${emoji}</div>`,
                    className: "",
                    iconSize: [26, 26],
                }),
            })
                .addTo(this.layer)
                .bindPopup(
                    `<b>${esc(r.label)}</b><br/>${esc(r.partner)}<br/>` +
                        `Último reporte: ${esc(r.last.time)}<br/>` +
                        `<a href="https://maps.google.com/?q=${r.last.lat},${r.last.lng}" target="_blank">Abrir en Google Maps</a>`
                );
            for (const ev of r.events || []) {
                // En vivo el inicio/fin ensucian; en Rutas cuentan la historia.
                if (this.mode === "live" && (ev.type === "inicio" || ev.type === "fin")) {
                    continue;
                }
                L.marker([ev.lat, ev.lng], {
                    icon: L.divIcon({
                        html: `<div style="font-size:18px">${EVENT_ICONS[ev.type] || "•"}</div>`,
                        className: "",
                        iconSize: [18, 18],
                    }),
                })
                    .addTo(this.layer)
                    .bindTooltip(`${esc(ev.type)} ${esc(ev.time)}`);
            }
        }
        if (!this.fitted && bounds.length) {
            this.map.fitBounds(bounds, { padding: [40, 40], maxZoom: 13 });
            this.fitted = true;
        }
    }
}

registry.category("actions").add("sale_delivery_wizard.live_map", DeliveryLiveMap);
