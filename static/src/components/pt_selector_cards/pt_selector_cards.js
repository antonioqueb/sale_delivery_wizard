/** @odoo-module */
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, useState, onWillStart } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

export class PtSelectorCards extends Component {
    static template = "sale_delivery_wizard.PtSelectorCards";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.state = useState({
            pts: [],
            isLoading: true,
            loadingPtId: null,
        });

        onWillStart(async () => {
            await this.loadPts();
        });
    }

    async loadPts() {
        this.state.isLoading = true;
        try {
            const record = this.props.record;
            const field = record.data[this.props.name];
            let ptIds = [];

            if (field) {
                if (Array.isArray(field.currentIds)) {
                    ptIds = field.currentIds;
                } else if (Array.isArray(field.records)) {
                    ptIds = field.records.map((r) => r.resId);
                } else if (field.resIds) {
                    ptIds = field.resIds;
                }
            }

            if (ptIds.length > 0) {
                const pts = await this.orm.read(
                    "sale.delivery.document",
                    ptIds,
                    [
                        "name",
                        "create_date",
                        "create_uid",
                        "total_qty",
                        "special_instructions",
                        "line_ids",
                    ]
                );
                this.state.pts = pts.sort(
                    (a, b) =>
                        new Date(b.create_date) - new Date(a.create_date)
                );
            } else {
                this.state.pts = [];
            }
        } catch (e) {
            console.error("[PT_SELECTOR_CARDS] Error loading PTs:", e);
            this.state.pts = [];
        } finally {
            this.state.isLoading = false;
        }
    }

    async onCardClick(pt) {
        if (this.state.loadingPtId) return; // prevenir doble click
        const wizardId = this._getWizardId();
        if (!wizardId) {
            console.error("[PT_SELECTOR_CARDS] No wizard ID encontrado");
            return;
        }

        this.state.loadingPtId = pt.id;
        try {
            const result = await this.orm.call(
                "sale.delivery.wizard",
                "action_load_pt_by_id",
                [[wizardId], pt.id]
            );
            if (result && typeof result === "object" && result.type) {
                await this.action.doAction(result);
            }
        } catch (e) {
            console.error("[PT_SELECTOR_CARDS] Error cargando PT:", e);
            this.state.loadingPtId = null;
        }
    }

    _getWizardId() {
        const rec = this.props.record;
        if (rec.resId && typeof rec.resId === "number" && rec.resId > 0) {
            return rec.resId;
        }
        const root = rec.model?.root;
        if (root?.resId && typeof root.resId === "number" && root.resId > 0) {
            return root.resId;
        }
        return null;
    }

    formatDate(dateStr) {
        if (!dateStr) return "—";
        try {
            const d = new Date(dateStr);
            return d.toLocaleString("es-MX", {
                day: "2-digit",
                month: "short",
                year: "numeric",
                hour: "2-digit",
                minute: "2-digit",
                hour12: false,
            });
        } catch (e) {
            return dateStr;
        }
    }

    getUserName(createUid) {
        if (!createUid) return "—";
        if (Array.isArray(createUid)) return createUid[1] || "—";
        return String(createUid);
    }

    getLotCount(lineIds) {
        if (!lineIds) return 0;
        if (Array.isArray(lineIds)) return lineIds.length;
        return 0;
    }

    fmtQty(num) {
        if (num === null || num === undefined || isNaN(num)) return "0.00";
        return parseFloat(num).toFixed(2);
    }

    truncate(text, len) {
        if (!text) return "";
        return text.length > len ? text.slice(0, len) + "…" : text;
    }
}

registry.category("fields").add("pt_selector_cards", {
    component: PtSelectorCards,
    displayName: "PT Selector Cards",
    supportedTypes: ["many2many"],
});