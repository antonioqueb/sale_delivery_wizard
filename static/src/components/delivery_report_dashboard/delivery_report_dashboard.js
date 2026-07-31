/** @odoo-module **/
// Reportería de Entregas — dashboard operativo.
// Fuentes: remisiones (m² por línea), vehículos de flota (capacidad m² y
// odómetro), choferes y telemetría GPS de los viajes (km, velocidades,
// tiempo detenido). Chart.js viene del bundle nativo de Odoo (lazy).
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { loadBundle } from "@web/core/assets";
import { Component, onMounted, onPatched, onWillUnmount, useRef, useState } from "@odoo/owl";

const PALETTE = ["#0B57D0", "#00B894", "#E5484D", "#F5A623", "#7C3AED",
    "#0891B2", "#DB2777", "#65A30D", "#EA580C", "#475569"];

export class DeliveryReportDashboard extends Component {
    static template = "sale_delivery_wizard.DeliveryReportDashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            days: 30,
            loading: true,
            data: null,
        });
        this.charts = {};
        this.refs = {
            daily: useRef("chartDaily"),
            util: useRef("chartUtil"),
            drivers: useRef("chartDrivers"),
            returns: useRef("chartReturns"),
            customers: useRef("chartCustomers"),
        };
        this._chartsPending = false;
        onMounted(async () => {
            await loadBundle("web.chartjs_lib");
            await this.load();
        });
        // OWL garantiza DOM actualizado en onPatched: aquí los <canvas>
        // ya existen. Un requestAnimationFrame suelto tras load() corre
        // ANTES del patch de OWL y deja las gráficas vacías para siempre.
        onPatched(() => {
            if (this._chartsPending) {
                this._chartsPending = false;
                this.renderCharts();
            }
        });
        onWillUnmount(() => this.destroyCharts());
    }

    destroyCharts() {
        for (const c of Object.values(this.charts)) {
            c.destroy();
        }
        this.charts = {};
    }

    async setPeriod(days) {
        if (this.state.days === days && this.state.data) {
            return;
        }
        this.state.days = days;
        await this.load();
    }

    async load() {
        this.state.loading = true;
        let data;
        try {
            data = await this.orm.call(
                "sale.delivery.live.map",
                "get_report_data",
                [this.state.days]
            );
        } catch {
            this.state.loading = false;
            return;
        }
        this._chartsPending = true;
        this.state.data = data;
        this.state.loading = false;
    }

    mk(key, ref, config) {
        if (this.charts[key]) {
            this.charts[key].destroy();
            delete this.charts[key];
        }
        if (!ref.el || !window.Chart) {
            return;
        }
        this.charts[key] = new window.Chart(ref.el.getContext("2d"), config);
    }

    renderCharts() {
        const d = this.state.data;
        if (!d) {
            return;
        }
        const grid = { color: "rgba(100,116,139,.12)" };

        // ── Entregas por día: barras (viajes) + línea (m²) ──────
        this.mk("daily", this.refs.daily, {
            type: "bar",
            data: {
                labels: d.daily.map((x) => x.date),
                datasets: [
                    {
                        type: "line",
                        label: "m² entregados",
                        data: d.daily.map((x) => Math.round(x.m2 * 10) / 10),
                        borderColor: "#00B894",
                        backgroundColor: "rgba(0,184,148,.15)",
                        fill: true,
                        tension: 0.35,
                        yAxisID: "y1",
                        pointRadius: 2,
                    },
                    {
                        type: "line",
                        label: "Piezas entregadas",
                        data: d.daily.map((x) => Math.round((x.units || 0) * 10) / 10),
                        borderColor: "#F5A623",
                        backgroundColor: "rgba(245,166,35,.12)",
                        fill: false,
                        tension: 0.35,
                        yAxisID: "y1",
                        pointRadius: 2,
                    },
                    {
                        label: "Viajes",
                        data: d.daily.map((x) => x.trips),
                        backgroundColor: "rgba(11,87,208,.75)",
                        borderRadius: 4,
                        yAxisID: "y",
                        maxBarThickness: 26,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: "index", intersect: false },
                scales: {
                    y: { beginAtZero: true, ticks: { precision: 0 }, grid },
                    y1: { beginAtZero: true, position: "right", grid: { display: false } },
                    x: { grid: { display: false } },
                },
            },
        });

        // ── Aprovechamiento de capacidad por vehículo ───────────
        const vehs = d.vehicles.filter((v) => v.trips);
        this.mk("util", this.refs.util, {
            type: "bar",
            data: {
                labels: vehs.map((v) => v.name),
                datasets: [
                    {
                        label: "% capacidad usada (promedio por viaje)",
                        data: vehs.map((v) => v.avg_util),
                        backgroundColor: vehs.map((v) =>
                            v.avg_util >= 85 ? "#00B894" :
                            v.avg_util >= 60 ? "#0B57D0" :
                            v.avg_util >= 40 ? "#F5A623" : "#E5484D"
                        ),
                        borderRadius: 5,
                        maxBarThickness: 34,
                    },
                ],
            },
            options: {
                indexAxis: "y",
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { beginAtZero: true, max: 100, grid,
                         ticks: { callback: (v) => v + "%" } },
                    y: { grid: { display: false } },
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: (ctx) => {
                                const v = vehs[ctx.dataIndex];
                                return ` ${v.avg_util}% de ${v.capacity} m² · ${v.trips} viajes · ${v.m2} m²` + (v.units ? ` · ${v.units} pzas` : "");
                            },
                        },
                    },
                },
            },
        });

        // ── Ranking de choferes por m² ──────────────────────────
        const drvs = d.drivers.slice(0, 8);
        this.mk("drivers", this.refs.drivers, {
            type: "bar",
            data: {
                labels: drvs.map((x) => x.name),
                datasets: [
                    {
                        label: "m² entregados",
                        data: drvs.map((x) => x.m2),
                        backgroundColor: "rgba(124,58,237,.8)",
                        borderRadius: 5,
                        maxBarThickness: 34,
                    },
                ],
            },
            options: {
                indexAxis: "y",
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { beginAtZero: true, grid },
                    y: { grid: { display: false } },
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: (ctx) => {
                                const x = drvs[ctx.dataIndex];
                                return ` ${x.m2} m²` + (x.units ? ` · ${x.units} pzas` : "") + ` · ${x.trips} viajes · ${x.km} km · ⌀ ${x.avg_kmh} km/h`;
                            },
                        },
                    },
                },
            },
        });

        // ── Devoluciones por motivo ─────────────────────────────
        this.mk("returns", this.refs.returns, {
            type: "doughnut",
            data: {
                labels: d.returns.map((x) => x.reason),
                datasets: [{
                    data: d.returns.map((x) => x.count),
                    backgroundColor: d.returns.map((_, i) => PALETTE[i % PALETTE.length]),
                    borderWidth: 2,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                cutout: "58%",
                plugins: { legend: { position: "right" } },
            },
        });

        // ── Top clientes por m² ─────────────────────────────────
        const custs = d.customers;
        this.mk("customers", this.refs.customers, {
            type: "bar",
            data: {
                labels: custs.map((x) => x.name),
                datasets: [{
                    label: "m² entregados",
                    data: custs.map((x) => Math.round(x.m2 * 10) / 10),
                    backgroundColor: "rgba(8,145,178,.8)",
                    borderRadius: 5,
                    maxBarThickness: 34,
                }],
            },
            options: {
                indexAxis: "y",
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { beginAtZero: true, grid },
                    y: { grid: { display: false } },
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: (ctx) =>
                                ` ${custs[ctx.dataIndex].m2} m²` +
                                (custs[ctx.dataIndex].units ? ` · ${custs[ctx.dataIndex].units} pzas` : "") +
                                ` · ${custs[ctx.dataIndex].trips} viajes`,
                        },
                    },
                },
            },
        });
    }
}

registry
    .category("actions")
    .add("sale_delivery_wizard.report_dashboard", DeliveryReportDashboard);
