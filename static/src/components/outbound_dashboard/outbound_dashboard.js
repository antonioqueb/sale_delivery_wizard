/** @odoo-module **/
// Tablero de Salidas — tablero de trabajo del operador de entregas.
//
// Flujo secuencial en carriles, de izquierda a derecha:
//   1 Por preparar (PT borrador) → 2 Listo para cargar (PT preparado)
//   → 3 En ruta (remisión confirmada) → 4 Entregadas hoy (firmadas).
// Las tarjetas se ARRASTRAN entre carriles; el servidor (board_move) decide
// qué transición es válida y con qué permisos. El panel lateral "Camiones"
// es destino de arrastre para asignar vehículo. La operación de campo sigue
// en el teléfono; aquí se planifica, se asigna y se cierra.
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";

const REFRESH_MS = 30000;

export const STAGES = [
    { key: "pending", step: 1, label: "Por preparar", hint: "Pick tickets por surtir", icon: "clipboard", tone: "slate" },
    { key: "ready", step: 2, label: "Listo para cargar", hint: "Material surtido, falta remisión", icon: "scan", tone: "strong" },
    { key: "in_route", step: 3, label: "En ruta", hint: "Remisiones de hoy en la calle", icon: "truck", tone: "sky" },
    { key: "delivered", step: 4, label: "Entregadas hoy", hint: "Firmadas por el cliente", icon: "check", tone: "green" },
];

// Transiciones que el tablero acepta por arrastre (el servidor las vuelve a validar).
const MOVES = {
    pending: ["ready"],
    ready: ["in_route"],
    in_route: ["delivered"],
    delivered: [],
};

export class OutboundDashboard extends Component {
    static template = "sale_delivery_wizard.OutboundDashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.stages = STAGES;
        this.state = useState({
            loading: true,
            data: null,
            search: "",
            filter: "all", // all | noveh | auth | today
            trucksOpen: true,
            drag: null, // { id, from, docType }
            over: null, // carril o "truck:<id>" resaltado
            busy: false,
            assignFor: null, // id de tarjeta con el selector de camión abierto
        });
        this.timer = null;
        this.lastPayload = null;
        onMounted(async () => {
            await this.load();
            this.timer = setInterval(() => {
                if (!this.state.drag && !this.state.busy) {
                    this.load();
                }
            }, REFRESH_MS);
        });
        onWillUnmount(() => {
            if (this.timer) {
                clearInterval(this.timer);
            }
        });
    }

    // ------------------------------------------------------------------
    // Datos
    // ------------------------------------------------------------------
    async load() {
        let data;
        try {
            data = await this.orm.call("sale.delivery.live.map", "get_outbound_dashboard_data", []);
        } catch (e) {
            console.error("[SALIDAS] no se pudo cargar el tablero", e);
            this.state.loading = false;
            return;
        }
        const payload = JSON.stringify(data);
        if (payload === this.lastPayload) {
            this.state.loading = false;
            return;
        }
        this.lastPayload = payload;
        this.state.data = data;
        this.state.loading = false;
    }

    refresh() {
        this.lastPayload = null;
        return this.load();
    }

    get data() {
        return this.state.data || { kpis: {}, pending: [], ready: [], in_route: [], delivered: [], fleet: [], trucks: [] };
    }

    matches(card) {
        const f = this.state.filter;
        if (f === "noveh" && card.vehicle) {
            return false;
        }
        if (f === "auth" && (card.auth_ok || !card.auth)) {
            return false;
        }
        if (f === "today" && !card.is_today) {
            return false;
        }
        const term = this.state.search.trim().toLowerCase();
        if (!term) {
            return true;
        }
        const hay = `${card.name} ${card.order} ${card.partner} ${card.vehicle} ${card.driver} ${(card.materials || []).map((m) => m.product).join(" ")}`;
        return hay.toLowerCase().includes(term);
    }

    cards(stageKey) {
        return (this.data[stageKey] || []).filter((c) => this.matches(c));
    }

    stageM2(stageKey) {
        return Math.round((this.data[stageKey] || []).reduce((acc, c) => acc + (c.m2 || 0), 0) * 10) / 10;
    }

    get noVehicleCount() {
        return this.data.kpis.no_vehicle || 0;
    }

    get pendingAuthCount() {
        return [...(this.data.pending || []), ...(this.data.ready || [])].filter((c) => c.auth && !c.auth_ok).length;
    }

    get todayCount() {
        return this.data.kpis.pts_today || 0;
    }

    get fleetLoaded() {
        return (this.data.fleet || []).filter((v) => v.m2 > 0 || v.docs.length);
    }

    get fleetIdle() {
        return (this.data.fleet || []).filter((v) => !(v.m2 > 0 || v.docs.length));
    }

    ageLabel(card) {
        const m = card.age_min || 0;
        if (m < 60) {
            return `hace ${m} min`;
        }
        const h = Math.floor(m / 60);
        if (h < 24) {
            return `hace ${h} h`;
        }
        const d = Math.floor(h / 24);
        return d === 1 ? "hace 1 día" : `hace ${d} días`;
    }

    ageCls(card) {
        const m = card.age_min || 0;
        if (m >= 48 * 60) {
            return "late";
        }
        if (m >= 24 * 60) {
            return "warn";
        }
        return "";
    }

    loadCls(pct) {
        if (pct > 100) {
            return "over";
        }
        if (pct >= 75) {
            return "high";
        }
        if (pct >= 40) {
            return "mid";
        }
        return "low";
    }

    // ------------------------------------------------------------------
    // Filtros y UI
    // ------------------------------------------------------------------
    setFilter(key) {
        this.state.filter = this.state.filter === key ? "all" : key;
    }

    onSearch(ev) {
        this.state.search = ev.target.value;
    }

    toggleTrucks() {
        this.state.trucksOpen = !this.state.trucksOpen;
    }

    toggleAssign(card) {
        this.state.assignFor = this.state.assignFor === card.id ? null : card.id;
    }

    // ------------------------------------------------------------------
    // Arrastrar y soltar
    // ------------------------------------------------------------------
    canMove(from, to) {
        return (MOVES[from] || []).includes(to);
    }

    onDragStart(ev, card, from) {
        this.state.drag = { id: card.id, from, docType: card.doc_type };
        ev.dataTransfer.effectAllowed = "move";
        try {
            ev.dataTransfer.setData("text/plain", String(card.id));
        } catch {
            // Safari viejo: sin setData sigue funcionando el estado interno.
        }
    }

    onDragEnd() {
        this.state.drag = null;
        this.state.over = null;
    }

    onLaneDragOver(ev, stageKey) {
        if (!this.state.drag || !this.canMove(this.state.drag.from, stageKey)) {
            return;
        }
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        this.state.over = stageKey;
    }

    onLaneDragLeave(ev, stageKey) {
        if (this.state.over === stageKey && !ev.currentTarget.contains(ev.relatedTarget)) {
            this.state.over = null;
        }
    }

    async onLaneDrop(ev, stageKey) {
        ev.preventDefault();
        const drag = this.state.drag;
        this.state.over = null;
        this.state.drag = null;
        if (!drag || !this.canMove(drag.from, stageKey)) {
            if (drag && drag.from !== stageKey) {
                this.notification.add("Ese movimiento no existe en el flujo: sigue el orden de los carriles.", { type: "warning" });
            }
            return;
        }
        await this.moveCard(drag.id, stageKey);
    }

    onTruckDragOver(ev, vehicle) {
        if (!this.state.drag || this.state.drag.from === "delivered") {
            return;
        }
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "link";
        this.state.over = `truck:${vehicle.id}`;
    }

    onTruckDragLeave(ev, vehicle) {
        if (this.state.over === `truck:${vehicle.id}` && !ev.currentTarget.contains(ev.relatedTarget)) {
            this.state.over = null;
        }
    }

    async onTruckDrop(ev, vehicle) {
        ev.preventDefault();
        const drag = this.state.drag;
        this.state.over = null;
        this.state.drag = null;
        if (!drag || drag.from === "delivered") {
            return;
        }
        await this.assignVehicle(drag.id, vehicle.id);
    }

    // ------------------------------------------------------------------
    // Acciones
    // ------------------------------------------------------------------
    async moveCard(docId, target) {
        if (this.state.busy) {
            return;
        }
        this.state.busy = true;
        try {
            const res = await this.orm.call("sale.delivery.live.map", "board_move", [docId, target]);
            if (res && res.action) {
                await this.action.doAction(res.action, { onClose: () => this.load() });
            } else if (res && res.error) {
                this.notification.add(res.error, { type: "danger" });
            } else if (res && res.message) {
                this.notification.add(res.message, { type: "success" });
            }
            this.lastPayload = null;
            await this.load();
        } catch (e) {
            console.error("[SALIDAS] no se pudo mover la tarjeta", e);
            this.notification.add("No se pudo mover el documento. Intenta de nuevo.", { type: "danger" });
        } finally {
            this.state.busy = false;
        }
    }

    async assignVehicle(docId, vehicleId) {
        if (this.state.busy) {
            return;
        }
        this.state.busy = true;
        try {
            const res = await this.orm.call("sale.delivery.live.map", "board_assign_vehicle", [docId, vehicleId || false]);
            if (res && res.error) {
                this.notification.add(res.error, { type: "danger" });
            }
            this.state.assignFor = null;
            this.lastPayload = null;
            await this.load();
        } catch (e) {
            console.error("[SALIDAS] no se pudo asignar el camión", e);
            this.notification.add("No se pudo asignar el camión.", { type: "danger" });
        } finally {
            this.state.busy = false;
        }
    }

    onAssignSelect(ev, card) {
        const value = ev.target.value;
        this.assignVehicle(card.id, value ? parseInt(value, 10) : false);
    }

    // Abrir = el PDF del documento en una pestaña nueva (sin descargar):
    // el almacén trabaja con el papel, no con el formulario de Odoo.
    openPdf(docType, id) {
        const report =
            docType === "pick_ticket"
                ? "sale_delivery_wizard.report_pick_ticket"
                : "sale_delivery_wizard.report_remission";
        window.open(`/report/pdf/${report}/${id}`, "_blank");
    }

    openDoc(id) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "sale.delivery.document",
            res_id: id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openOrder(id) {
        if (!id) {
            return;
        }
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "sale.order",
            res_id: id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openPtList() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Pick Tickets",
            res_model: "sale.delivery.document",
            domain: [["document_type", "=", "pick_ticket"]],
            views: [[false, "list"], [false, "form"]],
            target: "current",
        });
    }

    openRemissionList() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Remisiones",
            res_model: "sale.delivery.document",
            domain: [["document_type", "=", "remission"]],
            views: [[false, "list"], [false, "form"]],
            target: "current",
        });
    }

    openLiveMap() {
        this.action.doAction("sale_delivery_wizard.action_delivery_live_map_client");
    }
}

registry
    .category("actions")
    .add("sale_delivery_wizard.outbound_dashboard", OutboundDashboard);
