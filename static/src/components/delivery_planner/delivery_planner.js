/** @odoo-module **/
// Planificación de entregas — tablero operativo 100 % propio (no es el
// calendario nativo) con tres vistas:
//   · Día: una sola fecha a todo lo ancho, agrupada por horario, tarjetas
//     desplegadas (lo que el operador trabaja).
//   · Semana: siete columnas, una por día (lo que se planea).
//   · Mes: cuadrícula de calendario con fichas compactas (lo que se ve venir).
// En las tres se reprograma ARRASTRANDO a otro día (motivo obligatorio,
// historial y aviso al vendedor). El vendedor usa el mismo componente con
// params.only_mine desde "Mis Entregas".
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";

const REFRESH_MS = 60000;
const MONTHS = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];
const MONTHS_LONG = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"];
const DAYS_LONG = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"];
const DAYS_SHORT = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];
const OPEN = ["scheduled", "confirmed", "in_progress"];

function toIso(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
}
function fromIso(iso) {
    const [y, m, d] = iso.split("-").map((x) => parseInt(x, 10));
    return new Date(y, m - 1, d);
}
function addDays(iso, n) {
    const d = fromIso(iso);
    d.setDate(d.getDate() + n);
    return toIso(d);
}
function mondayOf(iso) {
    const d = fromIso(iso);
    const wd = (d.getDay() + 6) % 7; // lunes = 0
    d.setDate(d.getDate() - wd);
    return toIso(d);
}

export class DeliveryPlanner extends Component {
    static template = "sale_delivery_wizard.DeliveryPlanner";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.onlyMine = Boolean(this.props.action && this.props.action.params && this.props.action.params.only_mine);
        this.daysLong = DAYS_LONG;
        this.daysShort = DAYS_SHORT;
        let savedMode = "week";
        try {
            savedMode = window.localStorage.getItem("som_planner_mode") || "week";
        } catch {
            // sin storage: semana
        }
        this.state = useState({
            loading: true,
            data: null,
            mode: ["day", "week", "month"].includes(savedMode) ? savedMode : "week",
            anchor: toIso(new Date()), // fecha de referencia de la vista
            search: "",
            onlyIssues: false,
            drag: null, // { id, name, order, from }
            over: null, // iso del día resaltado
            move: null, // { id, order, from, to, reason }
            expanded: {},
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
    // Rango según la vista
    // ------------------------------------------------------------------
    get range() {
        const a = this.state.anchor;
        if (this.state.mode === "day") {
            return { from: a, to: a };
        }
        if (this.state.mode === "week") {
            const from = mondayOf(a);
            return { from, to: addDays(from, 6) };
        }
        const d = fromIso(a);
        const first = toIso(new Date(d.getFullYear(), d.getMonth(), 1));
        const last = toIso(new Date(d.getFullYear(), d.getMonth() + 1, 0));
        const from = mondayOf(first);
        const lastD = fromIso(last);
        const pad = (7 - ((lastD.getDay() + 6) % 7) - 1) % 7;
        return { from, to: addDays(last, pad) };
    }

    get title() {
        const d = fromIso(this.state.anchor);
        if (this.state.mode === "day") {
            return `${DAYS_LONG[(d.getDay() + 6) % 7]} ${d.getDate()} de ${MONTHS_LONG[d.getMonth()].toLowerCase()} ${d.getFullYear()}`;
        }
        if (this.state.mode === "week") {
            return this.data.week_label || "";
        }
        return `${MONTHS_LONG[d.getMonth()]} ${d.getFullYear()}`;
    }

    get anchorMonth() {
        return fromIso(this.state.anchor).getMonth() + 1;
    }

    // ------------------------------------------------------------------
    // Datos
    // ------------------------------------------------------------------
    async load() {
        const r = this.range;
        try {
            this.state.data = await this.orm.call("sale.delivery.schedule", "planner_range", [r.from, r.to, this.onlyMine]);
        } catch (e) {
            console.error("[PLANIFICACIÓN] no se pudo cargar", e);
            this.notification.add("No se pudo cargar la planificación.", { type: "danger" });
        } finally {
            this.state.loading = false;
        }
    }

    get data() {
        return this.state.data || { days: [], week_label: "", overdue: 0, is_staff: false, can_generate: false, today: this.state.anchor };
    }

    get days() {
        return this.data.days || [];
    }

    get day() {
        return this.days[0] || { iso: this.state.anchor, cards: [], open_count: 0, done_count: 0, issues: 0 };
    }

    get weeks() {
        const out = [];
        for (let i = 0; i < this.days.length; i += 7) {
            out.push(this.days.slice(i, i + 7));
        }
        return out;
    }

    get totals() {
        let open = 0;
        let done = 0;
        let issues = 0;
        for (const d of this.days) {
            if (this.state.mode === "month" && d.month !== this.anchorMonth) {
                continue;
            }
            open += d.open_count;
            done += d.done_count;
            issues += d.issues;
        }
        return { open, done, issues };
    }

    setMode(mode) {
        if (this.state.mode === mode) {
            return;
        }
        this.state.mode = mode;
        try {
            window.localStorage.setItem("som_planner_mode", mode);
        } catch {
            // sin storage
        }
        this.state.loading = true;
        this.load();
    }

    shift(delta) {
        const d = fromIso(this.state.anchor);
        if (this.state.mode === "day") {
            d.setDate(d.getDate() + delta);
        } else if (this.state.mode === "week") {
            d.setDate(d.getDate() + delta * 7);
        } else {
            d.setDate(1);
            d.setMonth(d.getMonth() + delta);
        }
        this.state.anchor = toIso(d);
        this.state.loading = true;
        this.load();
    }

    goToday() {
        this.state.anchor = toIso(new Date());
        this.state.loading = true;
        this.load();
    }

    openDay(iso) {
        this.state.anchor = iso;
        if (this.state.mode !== "day") {
            this.setMode("day"); // setMode recarga con el ancla nueva
        } else {
            this.state.loading = true;
            this.load();
        }
    }

    /** Vista Día: franja de ±3 días alrededor del ancla (navegación y destino de arrastre). */
    dayOffset(off) {
        return addDays(this.state.anchor, off);
    }

    dayShort(iso) {
        const d = fromIso(iso);
        return DAYS_SHORT[(d.getDay() + 6) % 7];
    }

    dayNumber(iso) {
        return fromIso(iso).getDate();
    }

    onSearch(ev) {
        this.state.search = ev.target.value;
    }

    toggleIssues() {
        this.state.onlyIssues = !this.state.onlyIssues;
    }

    cardsOf(day) {
        const term = this.state.search.trim().toLowerCase();
        return (day.cards || []).filter((c) => {
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

    /** Vista Día: secciones por horario, en orden operativo. */
    get daySections() {
        const cards = this.cardsOf(this.day);
        const exact = cards.filter((c) => c.time_window === "exact").sort((a, b) => a.time_exact - b.time_exact);
        const sections = [
            { key: "am", label: "Mañana (9–13 h)", cards: cards.filter((c) => c.time_window === "am") },
            { key: "exact", label: "Hora exacta", cards: exact },
            { key: "pm", label: "Tarde (13–18 h)", cards: cards.filter((c) => c.time_window === "pm") },
            { key: "any", label: "Todo el día", cards: cards.filter((c) => c.time_window === "any") },
        ];
        return sections.filter((s) => s.cards.length);
    }

    toggleExpand(card) {
        this.state.expanded[card.id] = !this.state.expanded[card.id];
    }

    isExpanded(card) {
        return this.state.mode === "day" ? this.state.expanded[card.id] !== false : Boolean(this.state.expanded[card.id]);
    }

    // ------------------------------------------------------------------
    // Arrastre entre días → reprogramar con motivo
    // ------------------------------------------------------------------
    isOpen(card) {
        return OPEN.includes(card.state);
    }

    onDragStart(ev, card, dayIso) {
        if (!this.isOpen(card)) {
            ev.preventDefault();
            return;
        }
        this.state.drag = { id: card.id, name: card.name, order: card.order, from: dayIso };
        ev.dataTransfer.effectAllowed = "move";
        try {
            ev.dataTransfer.setData("text/plain", String(card.id));
        } catch {
            // Safari viejo
        }
    }

    onDragEnd() {
        this.state.drag = null;
        this.state.over = null;
    }

    onDayDragOver(ev, dayIso) {
        if (!this.state.drag || this.state.drag.from === dayIso) {
            return;
        }
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        this.state.over = dayIso;
    }

    onDayDragLeave(ev, dayIso) {
        if (this.state.over === dayIso && !ev.currentTarget.contains(ev.relatedTarget)) {
            this.state.over = null;
        }
    }

    onDayDrop(ev, dayIso) {
        ev.preventDefault();
        const drag = this.state.drag;
        this.state.over = null;
        this.state.drag = null;
        if (!drag || drag.from === dayIso) {
            return;
        }
        this.state.move = { id: drag.id, name: drag.name, order: drag.order, from: drag.from, to: dayIso, reason: "" };
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
        const d = fromIso(iso);
        return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
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
                ["state", "in", OPEN],
            ],
            views: [[false, "list"], [false, "form"]],
            target: "current",
        });
    }
}

registry.category("actions").add("sale_delivery_wizard.delivery_planner", DeliveryPlanner);
