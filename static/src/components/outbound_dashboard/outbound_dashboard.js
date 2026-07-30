/** @odoo-module **/
// Tablero de Salidas — la orden del día del almacén: pick tickets por
// trabajar, remisiones en ruta, entregas firmadas y la carga de cada
// camión contra su capacidad en m². La operación se ejecuta con el
// teléfono (escaneo físico); aquí se ve, se organiza y se imprime.
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";

export class OutboundDashboard extends Component {
    static template = "sale_delivery_wizard.OutboundDashboard";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({ loading: true, data: null });
        this.timer = null;
        this.lastPayload = null;
        onMounted(async () => {
            await this.load();
            this.timer = setInterval(() => this.load(), 30000);
        });
        onWillUnmount(() => {
            if (this.timer) {
                clearInterval(this.timer);
            }
        });
    }

    async load() {
        let data;
        try {
            data = await this.orm.call(
                "sale.delivery.live.map",
                "get_outbound_dashboard_data",
                []
            );
        } catch {
            this.state.loading = false;
            return;
        }
        const payload = JSON.stringify(data);
        if (payload === this.lastPayload) {
            return;
        }
        this.lastPayload = payload;
        this.state.data = data;
        this.state.loading = false;
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
}

registry
    .category("actions")
    .add("sale_delivery_wizard.outbound_dashboard", OutboundDashboard);
