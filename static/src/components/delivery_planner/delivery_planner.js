/** @odoo-module **/
// Planificación de entregas — calendario semanal operativo.
//
// Una columna por día (lunes a domingo). Cada tarjeta es una entrega
// programada por el VENDEDOR con la información completa. Logística la
// confirma, la reprograma arrastrándola a otro día (con motivo, queda
// historial y aviso al vendedor) y la ejecuta ("Generar entrega" abre el
// asistente de la orden). El vendedor ve el mismo tablero solo con lo suyo
// (params.only_mine) desde su menú "Mis Entregas".
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";

const REFRESH_MS = 60000;

export class DeliveryPlanner extends Component {
    static template = "sale_delivery_wizard.DeliveryPlanner";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.onlyMine = Boolean(this.props.action && this.props.action.params && this.props.action.params.only_mine);
        this.state = useState({
            loading: true,
            data: null,
            weekStart: null, // ISO; null = semana actual
            search: "",
            onlyIssues: false,
            drag: null, // { id, from }
            over: null, // iso del día resaltado
            move: null, // { id, name, from, to, reason }
            expanded: {}, // id → bool (tarjeta desplegada)
            busy: false,
        });
        this.timer = null;
        onWillStart(() => this.load());
        this.timer = setInterval(() => {
            if (!this.state.drag && !this.state.move && !this.state.busy) {
                this.load();
            }
        }, REFRESH_MS);
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
        try {
            this.state.data = await this.orm.call(
                "sale.delivery.schedule",
                "planner_data",
                [this.state.weekStart, this.onlyMine]
            );
        } catch (e) {
            console.error("[PLANIFICACIÓN] no se pudo cargar", e);
            this.notification.add("No se pudo cargar la planificación.", { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    get data() {
        return this.state.data || { days: [], week_label: "", overdue: 0, is_staff: false, can_generate: false };
    }

    get weekTotals() {
        let open = 0;
        let done = 0;
        let issues = 0;
        for (const d of this.data.days) {
            open += d.open_count;
            done += d.done_count;
            issues += d.issues;
        }
        return { open, done, issues };
    }

    shiftWeek(delta) {
        const base = this.state.weekStart ? new Date(this.state.weekStart + "T00:00:00") : new Date(this.data.week_start + "T00:00:00");
        base.setDate(base.getDate() + delta * 7);
        this.state.weekStart = base.toISOString().slice(0, 10);
        this.state.loading = true;
        this.load();
    }

    goToday() {
        this.state.weekStart = null;
        this.state.loading = true;
        this.load();
    }

    onSearch(ev) {
        this.state.search = ev.target.value;
    }

    toggleIssues() {
        this.state.onlyIssues = !this.state.onlyIssues;
    }

    cardsOf(day) {
        const term = this.state.search.trim().toLowerCase();
        return day.cards.filter((c) => {
            if (this.state.onlyIssues && !c.readiness) {
                return false;
            }
            if (!term) {
                return true;
            }
            const hay = `${c.name} ${c.order} ${c.partner} ${c.seller} ${c.contact} ${c.address} ${c.vehicle} ${c.driver}`;
            return hay.toLowerCase().includes(term);
        });
    }

    toggleExpand(card) {
        this.state.expanded[card.id] = !this.state.expanded[card.id];
    }

    // ------------------------------------------------------------------
    // Arrastre entre días → reprogramar con motivo
    // ------------------------------------------------------------------
    isOpen(card) {
        return ["scheduled", "confirmed", "in_progress"].includes(card.state);
    }

    onDragStart(ev, card, day) {
        if (!this.isOpen(card)) {
            ev.preventDefault();
            return;
        }
        this.state.drag = { id: card.id, name: card.name, order: card.order, from: day.iso };
        ev.dataTransfer.effectAllowed = "move";
        try {
            ev.dataTransfer.setData("text/plain", String(card.id));
        } catch {
            // Safari viejo: el estado interno basta.
        }
    }

    onDragEnd() {
        this.state.drag = null;
        this.state.over = null;
    }

    onDayDragOver(ev, day) {
        if (!this.state.drag || this.state.drag.from === day.iso) {
            return;
        }
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        this.state.over = day.iso;
    }

    onDayDragLeave(ev, day) {
        if (this.state.over === day.iso && !ev.currentTarget.contains(ev.relatedTarget)) {
            this.state.over = null;
        }
    }

    onDayDrop(ev, day) {
        ev.preventDefault();
        const drag = this.state.drag;
        this.state.over = null;
        this.state.drag = null;
        if (!drag || drag.from === day.iso) {
            return;
        }
        // Se pide motivo antes de mover: queda en el historial y se lo lleva el vendedor.
        this.state.move = { id: drag.id, name: drag.name, order: drag.order, from: drag.from, to: day.iso, reason: "" };
    }

    onMoveReason(ev) {
        if (this.state.move) {
            this.state.move.reason = ev.target.value;
        }
    }

    cancelMove() {
        this.state.move = null;
    }

    async confirmMove() {
        const mv = this.state.move;
        if (!mv || this.state.busy) {
            return;
        }
        if (!mv.reason.trim()) {
            this.notification.add("Escribe el motivo: el vendedor lo va a leer.", { type: "warning" });
            return;
        }
        this.state.busy = true;
        try {
            const res = await this.orm.call("sale.delivery.schedule", "planner_move", [mv.id, mv.to, mv.reason.trim()]);
            if (res && res.error) {
                this.notification.add(res.error, { type: "danger" });
            } else {
                this.notification.add(`${mv.order} reprogramada al ${this.fmtIso(mv.to)}.`, { type: "success" });
            }
            this.state.move = null;
            await this.load();
        } catch (e) {
            console.error("[PLANIFICACIÓN] no se pudo reprogramar", e);
            this.notification.add("No se pudo reprogramar.", { type: "danger" });
        } finally {
            this.state.busy = false;
        }
    }

    fmtIso(iso) {
        const MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];
        const [y, m, d] = iso.split("-").map((x) => parseInt(x, 10));
        return `${d} ${MONTHS[m - 1]} ${y}`;
    }

    // ------------------------------------------------------------------
    // Acciones
    // ------------------------------------------------------------------
    async confirmCard(card) {
        if (this.state.busy) {
            return;
        }
        this.state.busy = true;
        try {
            const res = await this.orm.call("sale.delivery.schedule", "planner_confirm", [card.id]);
            if (res && res.error) {
                this.notification.add(res.error, { type: "danger" });
            }
            await this.load();
        } finally {
            this.state.busy = false;
        }
    }

    async generateDelivery(card) {
        try {
            const action = await this.orm.call("sale.delivery.schedule", "action_generate_delivery", [[card.id]]);
            if (action) {
                await this.action.doAction(action, { onClose: () => this.load() });
            }
        } catch (e) {
            console.error("[PLANIFICACIÓN] no se pudo abrir la entrega", e);
        }
    }

    openSchedule(card) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "sale.delivery.schedule",
            res_id: card.id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openOrder(card) {
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "sale.order",
            res_id: card.order_id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    openMap(card) {
        if (card.has_location) {
            window.open(`https://maps.google.com/?q=${card.lat},${card.lng}`, "_blank");
        }
    }

    openOverdue() {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: "Entregas atrasadas",
            res_model: "sale.delivery.schedule",
            domain: [
                ["date", "<", this.data.today],
                ["state", "in", ["scheduled", "confirmed", "in_progress"]],
            ],
            views: [[false, "list"], [false, "form"]],
            target: "current",
        });
    }
}

registry.category("actions").add("sale_delivery_wizard.delivery_planner", DeliveryPlanner);
