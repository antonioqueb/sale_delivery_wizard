/** @odoo-module */
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, useState, onWillStart, onWillUpdateProps } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

export class DeliveryGroupedList extends Component {
    static template = "sale_delivery_wizard.DeliveryGroupedList";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");

        this.state = useState({
            groups: [],
            collapsed: {},
            isLoading: true,
            mode: "delivery",
        });

        this._wizardModel = "";
        this._lineModel = "";
        this._wizardId = null;

        onWillStart(async () => {
            this._detectMode();
            await this._buildGroups();
        });

        onWillUpdateProps(async () => {
            await this._buildGroups();
        });
    }

    _detectMode() {
        const model = this.props.record?.model?.config?.resModel || "";
        this._wizardModel = model;
        if (model.includes("return")) {
            this.state.mode = "return";
            this._lineModel = "sale.return.wizard.line";
        } else if (model.includes("swap")) {
            this.state.mode = "swap";
            this._lineModel = "sale.swap.wizard.line";
        } else {
            this.state.mode = "delivery";
            this._lineModel = "sale.delivery.wizard.line";
        }
    }

    async _buildGroups() {
        this.state.isLoading = true;
        try {
            // Strategy: ensure wizard is saved, then read lines via ORM
            await this._ensureWizardSaved();

            if (!this._wizardId) {
                console.warn("[DGL] No wizard ID available");
                this.state.groups = [];
                return;
            }

            const lineData = await this._readLinesFromDb();
            this._groupLineData(lineData);

            if (Object.keys(this.state.collapsed).length === 0) {
                for (const g of this.state.groups) {
                    this.state.collapsed[g.productId] = false;
                }
            }
        } catch (e) {
            console.error("[DGL] _buildGroups error:", e);
            this.state.groups = [];
        } finally {
            this.state.isLoading = false;
        }
    }

    async _ensureWizardSaved() {
        // Check if already saved
        const root = this.props.record.model?.root || this.props.record;
        if (root.resId) {
            this._wizardId = root.resId;
            return;
        }

        try {
            // Use urgentSave if available (doesn't reload the form)
            if (typeof root.urgentSave === "function") {
                await root.urgentSave();
            } else {
                await root.save({ noReload: true, stayInEdition: true });
            }
            this._wizardId = root.resId || false;
        } catch (e) {
            console.warn("[DGL] save failed:", e.message);
        }

        if (!this._wizardId) {
            // Last resort: check if resId appeared after save
            this._wizardId = root.resId || null;
        }
    }

    async _readLinesFromDb() {
        const fields = this._getFieldsToRead();
        try {
            return await this.orm.searchRead(
                this._lineModel,
                [["wizard_id", "=", this._wizardId]],
                fields,
                { order: "sequence, id" }
            );
        } catch (e) {
            console.error("[DGL] searchRead failed:", e);
            return [];
        }
    }

    _getFieldsToRead() {
        const common = ["id", "display_type", "section_name", "sequence", "product_id"];
        if (this.state.mode === "delivery") {
            return [
                ...common,
                "lot_id", "source_location_id", "is_selected",
                "qty_available", "qty_to_deliver",
                "product_name", "lot_name",
                "picking_id", "move_id", "move_line_id", "sale_line_id",
            ];
        } else if (this.state.mode === "return") {
            return [
                ...common,
                "lot_id", "is_selected", "qty_delivered", "qty_to_return",
                "move_id", "move_line_id", "sale_line_id",
            ];
        } else {
            return [
                ...common,
                "origin_lot_id", "target_lot_id",
                "origin_bloque", "origin_alto", "origin_ancho",
                "qty", "target_bloque", "target_qty",
                "move_line_id", "picking_id", "sale_line_id",
            ];
        }
    }

    _groupLineData(lineData) {
        const owlRecords = this._getOwlRecords();
        const grouped = new Map();
        let currentSectionName = "";
        let currentSectionProductId = 0;
        let owlIdx = 0;

        for (const d of lineData) {
            if (d.display_type === "line_section") {
                currentSectionProductId = d.product_id ? d.product_id[0] : 0;
                currentSectionName = d.section_name || "";
                owlIdx++;
                continue;
            }

            const productId = d.product_id ? d.product_id[0] : (currentSectionProductId || 0);
            let productName = d.product_id ? d.product_id[1] : "";
            if (!productName && d.product_name) productName = d.product_name;
            if (!productName && currentSectionName) productName = currentSectionName;
            if (!productName) productName = "Sin Producto";

            const groupKey = productId || productName;
            if (!grouped.has(groupKey)) {
                grouped.set(groupKey, {
                    productId: groupKey,
                    productName,
                    lines: [],
                    totalQty: 0,
                    selectedCount: 0,
                    lineCount: 0,
                });
            }

            const group = grouped.get(groupKey);
            const owlRecord = owlIdx < owlRecords.length ? owlRecords[owlIdx] : null;
            owlIdx++;

            const ld = {
                _record: owlRecord,
                _dbId: d.id,
                _lineModel: this._lineModel,
                id: d.id,
                owlId: owlRecord ? owlRecord.id : `db_${d.id}`,
                product_id: productId,
                product_name: productName,
                lot_id: d.lot_id ? d.lot_id[0] : 0,
                lot_name: d.lot_id ? d.lot_id[1] : (d.lot_name || ""),
                is_selected: d.is_selected || false,
                qty_available: d.qty_available || 0,
                qty_to_deliver: d.qty_to_deliver || 0,
                source_location: d.source_location_id ? d.source_location_id[1] : "",
                qty_delivered: d.qty_delivered || 0,
                qty_to_return: d.qty_to_return || 0,
                origin_lot_id: d.origin_lot_id ? d.origin_lot_id[0] : 0,
                origin_lot_name: d.origin_lot_id ? d.origin_lot_id[1] : "",
                origin_bloque: d.origin_bloque || "",
                origin_alto: d.origin_alto || "",
                origin_ancho: d.origin_ancho || "",
                qty: d.qty || 0,
                target_lot_id: d.target_lot_id ? d.target_lot_id[0] : 0,
                target_lot_name: d.target_lot_id ? d.target_lot_id[1] : "",
                target_bloque: d.target_bloque || "",
                target_qty: d.target_qty || 0,
            };

            group.lines.push(ld);
            group.lineCount++;

            if (this.state.mode === "delivery") {
                group.totalQty += ld.qty_to_deliver || 0;
                if (ld.is_selected) group.selectedCount++;
            } else if (this.state.mode === "return") {
                group.totalQty += ld.qty_to_return || 0;
                if (ld.is_selected) group.selectedCount++;
            } else {
                group.totalQty += ld.qty || 0;
            }
        }

        this.state.groups = Array.from(grouped.values());
    }

    _getOwlRecords() {
        const raw = this.props.record.data[this.props.name];
        if (!raw) return [];
        return raw.records || [];
    }

    // ─── Interactions ─────────────────────────────────────────────────────

    toggleGroup(productId) {
        this.state.collapsed[productId] = !this.state.collapsed[productId];
    }

    isCollapsed(productId) {
        return !!this.state.collapsed[productId];
    }

    expandAll() {
        for (const g of this.state.groups) this.state.collapsed[g.productId] = false;
    }

    collapseAll() {
        for (const g of this.state.groups) this.state.collapsed[g.productId] = true;
    }

    async _writeLine(lineData, updates) {
        if (lineData._dbId && lineData._lineModel) {
            await this.orm.write(lineData._lineModel, [lineData._dbId], updates);
        }
    }

    async toggleLineSelected(lineData) {
        const newVal = !lineData.is_selected;
        const updates = { is_selected: newVal };
        if (this.state.mode === "delivery") {
            updates.qty_to_deliver = newVal ? (lineData.qty_available || 0) : 0;
        } else if (this.state.mode === "return") {
            updates.qty_to_return = newVal ? (lineData.qty_delivered || 0) : 0;
        }
        await this._writeLine(lineData, updates);
        await this._buildGroups();
    }

    async onQtyChange(lineData, event) {
        const val = parseFloat(event.target.value) || 0;
        const updates = {};
        if (this.state.mode === "delivery") {
            updates.qty_to_deliver = val;
            updates.is_selected = val > 0;
        } else if (this.state.mode === "return") {
            updates.qty_to_return = val;
            updates.is_selected = val > 0;
        }
        await this._writeLine(lineData, updates);
        await this._buildGroups();
    }

    async selectAllInGroup(group) {
        for (const ld of group.lines) {
            const u = { is_selected: true };
            if (this.state.mode === "delivery") u.qty_to_deliver = ld.qty_available || 0;
            else if (this.state.mode === "return") u.qty_to_return = ld.qty_delivered || 0;
            await this._writeLine(ld, u);
        }
        await this._buildGroups();
    }

    async deselectAllInGroup(group) {
        for (const ld of group.lines) {
            const u = { is_selected: false };
            if (this.state.mode === "delivery") u.qty_to_deliver = 0;
            else if (this.state.mode === "return") u.qty_to_return = 0;
            await this._writeLine(ld, u);
        }
        await this._buildGroups();
    }

    // ─── Swap popup ───────────────────────────────────────────────────────

    openSwapSelector(lineData) {
        const productId = lineData.product_id;
        const originLotId = lineData.origin_lot_id;
        if (!productId) return;
        this._openSwapPopup(lineData, productId, originLotId);
    }

    _openSwapPopup(lineData, productId, originLotId) {
        const self = this;
        const root = document.createElement("div");
        root.className = "swap-popup-root";
        document.body.appendChild(root);

        const PAGE_SIZE = 35;
        const st = {
            quants: [], totalCount: 0, isLoading: false, page: 0,
            selectedLotId: lineData.target_lot_id || null,
            selectedLotName: lineData.target_lot_name || "",
            filters: { lot_name: "", bloque: "", atado: "" },
        };
        let searchTimeout = null;

        const cleanup = () => {
            if (root._kh) document.removeEventListener("keydown", root._kh);
            root.remove();
        };

        const updateUI = () => {
            const badge = root.querySelector("#dgl-sel-badge");
            const name = root.querySelector("#dgl-sel-name");
            const btns = root.querySelectorAll(".dgl-confirm-btn");
            if (st.selectedLotId) {
                badge.style.display = "";
                name.textContent = st.selectedLotName;
                btns.forEach(b => b.disabled = false);
            } else {
                badge.style.display = "none";
                btns.forEach(b => b.disabled = true);
            }
        };

        const render = () => {
            const body = root.querySelector("#dgl-body");
            const stat = root.querySelector("#dgl-stat");
            if (!st.quants.length && !st.isLoading) {
                body.innerHTML = `<div class="dgl-empty"><i class="fa fa-inbox fa-3x text-muted"></i><div class="mt-2">No hay lotes disponibles</div></div>`;
                stat.textContent = "0 lotes"; return;
            }
            let rows = "";
            for (const q of st.quants) {
                const lotId = q.lot_id ? q.lot_id[0] : 0;
                const lotName = q.lot_id ? q.lot_id[1] : "-";
                if (lotId === originLotId) continue;
                const sel = st.selectedLotId === lotId;
                const tipo = (q.x_tipo || "placa").toLowerCase();
                const loc = q.location_id ? q.location_id[1].split("/").pop() : "-";
                rows += `<tr class="${sel ? "dgl-row-sel" : ""}" data-lid="${lotId}" data-ln="${lotName.replace(/"/g, '&quot;')}">
                    <td class="text-center"><div class="dgl-radio ${sel ? "checked" : ""}">${sel ? '<i class="fa fa-check"></i>' : ""}</div></td>
                    <td class="dgl-cell-lot">${lotName}</td><td>${q.x_bloque || "-"}</td><td>${q.x_atado || "-"}</td>
                    <td class="text-end">${q.x_alto ? parseFloat(q.x_alto).toFixed(0) : "-"}</td>
                    <td class="text-end">${q.x_ancho ? parseFloat(q.x_ancho).toFixed(0) : "-"}</td>
                    <td class="text-end fw-bold">${q.quantity ? q.quantity.toFixed(2) : "0.00"}</td>
                    <td><span class="dgl-tag dgl-tag-${tipo}">${tipo}</span></td>
                    <td>${q.x_color || "-"}</td><td class="text-muted small">${loc}</td></tr>`;
            }
            body.innerHTML = `<table class="dgl-popup-table"><thead><tr>
                <th style="width:36px"></th><th>Lote</th><th>Bloque</th><th>Atado</th>
                <th class="text-end">Alto</th><th class="text-end">Ancho</th><th class="text-end">m²</th>
                <th>Tipo</th><th>Color</th><th>Ubic.</th></tr></thead><tbody>${rows}</tbody></table>`;
            stat.textContent = `${st.totalCount} lotes`;
            body.querySelectorAll("tr[data-lid]").forEach(tr => {
                tr.style.cursor = "pointer";
                tr.addEventListener("click", () => {
                    const id = parseInt(tr.dataset.lid);
                    if (st.selectedLotId === id) { st.selectedLotId = null; st.selectedLotName = ""; }
                    else { st.selectedLotId = id; st.selectedLotName = tr.dataset.ln; }
                    updateUI(); render();
                });
            });
        };

        const load = async (page, reset) => {
            if (reset) { st.quants = []; }
            st.isLoading = true;
            try {
                let result;
                try {
                    result = await self.orm.call("stock.quant", "search_stone_inventory_for_so_paginated", [], {
                        product_id: productId, filters: st.filters, current_lot_ids: [], page, page_size: PAGE_SIZE });
                } catch (_e) {
                    const all = await self.orm.call("stock.quant", "search_stone_inventory_for_so", [], {
                        product_id: productId, filters: st.filters, current_lot_ids: [] }) || [];
                    result = { items: all.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE), total: all.length };
                }
                st.quants = reset ? (result.items || []) : [...st.quants, ...(result.items || [])];
                st.totalCount = result.total || 0;
                st.page = page;
            } catch (e) { console.error("[DGL SWAP]", e); }
            finally { st.isLoading = false; }
            render();
        };

        const doConfirm = async () => {
            if (!st.selectedLotId) return;
            cleanup();
            if (lineData._dbId) {
                try {
                    await self.orm.write(lineData._lineModel || "sale.swap.wizard.line",
                        [lineData._dbId], { target_lot_id: st.selectedLotId });
                } catch (e) { console.warn("[DGL SWAP] write failed:", e); }
            }
            await self._buildGroups();
        };

        root.innerHTML = `<div class="dgl-overlay" id="dgl-overlay"><div class="dgl-popup">
            <div class="dgl-popup-header"><span><i class="fa fa-exchange me-2"></i>Seleccionar Lote de Reemplazo</span>
            <div class="d-flex align-items-center gap-2">
                <span class="dgl-origin-badge"><i class="fa fa-cube me-1"></i>Actual: <strong>${lineData.origin_lot_name}</strong></span>
                <span class="dgl-sel-badge" id="dgl-sel-badge" style="display:none"><i class="fa fa-arrow-right me-1"></i>Nuevo: <strong id="dgl-sel-name">—</strong></span>
                <button class="dgl-confirm-btn dgl-btn-green" disabled><i class="fa fa-check me-1"></i>Confirmar</button>
                <button class="dgl-close-btn"><i class="fa fa-times"></i></button>
            </div></div>
            <div class="dgl-popup-filters">
                <div class="dgl-fg"><label>Lote</label><input type="text" id="dglf-lot" placeholder="Buscar..."/></div>
                <div class="dgl-fg"><label>Bloque</label><input type="text" id="dglf-bloque" placeholder="Bloque..."/></div>
                <div class="dgl-fg"><label>Atado</label><input type="text" id="dglf-atado" placeholder="Atado..."/></div>
                <div class="dgl-spacer"></div><span id="dgl-stat" class="text-muted small">Buscando...</span>
            </div>
            <div class="dgl-popup-body" id="dgl-body"><div class="dgl-empty"><i class="fa fa-circle-o-notch fa-spin fa-2x text-muted"></i></div></div>
            <div class="dgl-popup-footer"><button class="dgl-btn-outline" id="dgl-cancel">Cancelar</button>
                <button class="dgl-confirm-btn dgl-btn-primary" disabled><i class="fa fa-exchange me-1"></i>Usar este lote</button>
            </div></div></div>`;

        root.querySelector(".dgl-close-btn").addEventListener("click", cleanup);
        root.querySelector("#dgl-cancel").addEventListener("click", cleanup);
        root.querySelector("#dgl-overlay").addEventListener("click", e => { if (e.target.id === "dgl-overlay") cleanup(); });
        root.querySelectorAll(".dgl-confirm-btn").forEach(b => b.addEventListener("click", doConfirm));
        root._kh = e => { if (e.key === "Escape") cleanup(); };
        document.addEventListener("keydown", root._kh);

        ["dglf-lot:lot_name", "dglf-bloque:bloque", "dglf-atado:atado"].forEach(p => {
            const [id, key] = p.split(":");
            const el = root.querySelector(`#${id}`);
            if (el) el.addEventListener("input", () => {
                st.filters[key] = el.value;
                if (searchTimeout) clearTimeout(searchTimeout);
                searchTimeout = setTimeout(() => load(0, true), 350);
            });
        });
        if (st.selectedLotId) updateUI();
        load(0, true);
    }

    async clearSwapTarget(lineData) {
        if (lineData._dbId) {
            try {
                await this.orm.write(lineData._lineModel || "sale.swap.wizard.line",
                    [lineData._dbId], { target_lot_id: false });
            } catch (e) {}
        }
        await this._buildGroups();
    }

    fmt(num) {
        if (num === null || num === undefined || isNaN(num)) return "0.00";
        return parseFloat(num).toFixed(2);
    }

    fmtDim(val) {
        if (!val) return "-";
        const v = parseFloat(val);
        return isNaN(v) ? "-" : (v % 1 === 0 ? v.toFixed(0) : v.toFixed(2));
    }
}

registry.category("fields").add("delivery_grouped_list", {
    component: DeliveryGroupedList,
    displayName: "Delivery Grouped List",
    supportedTypes: ["one2many"],
});