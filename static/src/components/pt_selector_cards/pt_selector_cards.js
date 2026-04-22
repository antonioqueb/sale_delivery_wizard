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
        this.notification = useService("notification");
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
            const ptIds = this._getPtIdsFromField();
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

    _getPtIdsFromField() {
        const field = this.props.record.data[this.props.name];
        if (!field) return [];

        // Varias formas que puede venir un many2many en Odoo 19
        if (Array.isArray(field.currentIds) && field.currentIds.length > 0) {
            return field.currentIds.filter((id) => typeof id === "number" && id > 0);
        }
        if (Array.isArray(field.resIds) && field.resIds.length > 0) {
            return field.resIds.filter((id) => typeof id === "number" && id > 0);
        }
        if (Array.isArray(field.records) && field.records.length > 0) {
            return field.records
                .map((r) => r.resId)
                .filter((id) => typeof id === "number" && id > 0);
        }
        if (Array.isArray(field) && field.length > 0) {
            return field.filter((id) => typeof id === "number" && id > 0);
        }
        return [];
    }

    async onCardClick(pt) {
        if (this.state.loadingPtId) return;
        this.state.loadingPtId = pt.id;

        try {
            // Los TransientModel no persisten hasta que se hace save().
            // Si no tenemos resId, primero persistimos el wizard.
            let wizardId = this._getWizardId();

            if (!wizardId) {
                console.log("[PT_SELECTOR_CARDS] Sin resId, forzando save del wizard...");
                try {
                    await this.props.record.save({ reload: false });
                } catch (saveErr) {
                    // Algunas versiones no aceptan opciones
                    try {
                        await this.props.record.save();
                    } catch (e2) {
                        console.warn("[PT_SELECTOR_CARDS] save() falló, intentando root.save()", e2);
                        const root = this.props.record.model?.root;
                        if (root && root.save) {
                            await root.save();
                        }
                    }
                }
                wizardId = this._getWizardId();
                console.log("[PT_SELECTOR_CARDS] Después de save, wizardId =", wizardId);
            }

            if (!wizardId) {
                this.notification.add(
                    "No se pudo persistir el wizard. Cierra y vuelve a abrir.",
                    { type: "danger", sticky: false }
                );
                this.state.loadingPtId = null;
                return;
            }

            console.log(
                "[PT_SELECTOR_CARDS] Llamando action_load_pt_by_id con wizardId=%s pt=%s",
                wizardId, pt.id
            );

            const result = await this.orm.call(
                "sale.delivery.wizard",
                "action_load_pt_by_id",
                [[wizardId], pt.id]
            );

            console.log("[PT_SELECTOR_CARDS] Resultado:", result);

            if (result && typeof result === "object" && result.type) {
                await this.action.doAction(result);
            } else {
                this.notification.add(
                    "El servidor no retornó una acción. Revisa la consola.",
                    { type: "warning" }
                );
                this.state.loadingPtId = null;
            }
        } catch (e) {
            console.error("[PT_SELECTOR_CARDS] Error:", e);
            const msg = (e && e.message) ? e.message : String(e);
            this.notification.add("Error al abrir Pick Ticket: " + msg, {
                type: "danger",
                sticky: true,
            });
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
        // Fallback: buscar en el modelo
        if (rec._values && typeof rec._values.id === "number" && rec._values.id > 0) {
            return rec._values.id;
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