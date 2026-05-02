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
        this._writeTimeout = null;
        this._initialized = false;

        onWillStart(async () => {
            this._detectMode();
            await this._loadGroups();
            this._initialized = true;
            this._writeSelectionsToRecord();
        });

        onWillUpdateProps(async () => {
            // No recargar automáticamente para no destruir el estado local
            // mientras el usuario selecciona lotes en el widget.
        });
    }

    _detectMode() {
        const model = this.props.record?.model?.config?.resModel || "";

        if (model.includes("return")) {
            this.state.mode = "return";
            this._wizardModel = "sale.return.wizard";
            this._lineModel = "sale.return.wizard.line";
        } else if (model.includes("swap")) {
            this.state.mode = "swap";
            this._wizardModel = "sale.swap.wizard";
            this._lineModel = "sale.swap.wizard.line";
        } else {
            this.state.mode = "delivery";
            this._wizardModel = "sale.delivery.wizard";
            this._lineModel = "sale.delivery.wizard.line";
        }
    }

    async _loadGroups() {
        this.state.isLoading = true;
        let loadedFromWizard = false;

        try {
            const wizardId = this._getWizardId();

            if (wizardId) {
                const groups = await this.orm.call(
                    this._wizardModel,
                    "get_grouped_lines_data",
                    [[wizardId]]
                );
                this.state.groups = groups || [];
                loadedFromWizard = true;
            } else {
                const soId = this._getSaleOrderId();

                if (soId) {
                    const editingPtId = this._getEditingPtId();
                    const kwargs = { mode: this.state.mode };

                    if (editingPtId) {
                        kwargs.editing_pt_id = editingPtId;
                    }

                    const groups = await this.orm.call(
                        "sale.order",
                        "get_delivery_grouped_data",
                        [[soId]],
                        kwargs
                    );
                    this.state.groups = groups || [];
                } else {
                    this.state.groups = [];
                }
            }

            this._syncCollapsedState();
            this._recalcAllGroups();

            if (!loadedFromWizard) {
                this._applyStoredSelections();
            }
        } catch (e) {
            console.error("[DGL] Load groups failed:", e);
            this.state.groups = [];
        } finally {
            this.state.isLoading = false;
        }
    }

    _applyStoredSelections() {
        if (this.state.mode !== "delivery") return;

        const root = this.props.record?.model?.root || this.props.record;
        const wsRaw = root?.data?.widget_selections;

        if (!wsRaw || wsRaw === "[]") return;

        try {
            const sels = JSON.parse(wsRaw);
            if (!Array.isArray(sels) || sels.length === 0) return;

            const selMap = new Map();

            for (const s of sels) {
                const key = `${s.productId || 0}-${s.lotId || 0}`;
                selMap.set(key, s);
            }

            let hasAnyMatch = false;

            for (const group of this.state.groups) {
                for (const line of group.lines) {
                    const key = `${line.productId || 0}-${line.lotId || 0}`;
                    const sel = selMap.get(key);

                    if (sel) {
                        line.isSelected = true;
                        line.qtyToDeliver = sel.qty || 0;
                        hasAnyMatch = true;
                    } else {
                        line.isSelected = false;
                        line.qtyToDeliver = 0;
                    }
                }
            }

            if (hasAnyMatch) {
                this._recalcAllGroups();
                this.state.groups = [...this.state.groups];
                console.log(
                    "[DGL] Applied stored selections from Pick Ticket (%d items)",
                    sels.length
                );
            }
        } catch (e) {
            console.warn("[DGL] Error applying stored selections:", e);
        }
    }

    _getWizardId() {
        const root = this.props.record?.model?.root || this.props.record;
        const rid = root?.resId || this.props.record?.resId || null;

        if (rid && typeof rid === "number" && rid > 0) return rid;

        return null;
    }

    _getSaleOrderId() {
        const root = this.props.record?.model?.root || this.props.record;
        const soField = root?.data?.sale_order_id;

        if (soField) {
            if (typeof soField === "number" && soField > 0) return soField;

            if (typeof soField === "object" && soField !== null) {
                if (typeof soField.resId === "number" && soField.resId > 0) {
                    return soField.resId;
                }
                if (typeof soField.id === "number" && soField.id > 0) {
                    return soField.id;
                }
                if (Array.isArray(soField) && soField[0] > 0) {
                    return soField[0];
                }
            }
        }

        const ctx = this.props.record?.model?.config?.context || {};
        return ctx.default_sale_order_id || ctx.active_id || null;
    }

    _getEditingPtId() {
        if (this.state.mode !== "delivery") return null;

        const root = this.props.record?.model?.root || this.props.record;
        const val = root?.data?.editing_pick_ticket_id;

        if (val) {
            if (typeof val === "number" && val > 0) return val;
            if (Array.isArray(val) && val[0] > 0) return val[0];

            if (typeof val === "object" && val !== null) {
                if (typeof val.resId === "number" && val.resId > 0) {
                    return val.resId;
                }
                if (typeof val.id === "number" && val.id > 0) {
                    return val.id;
                }
            }
        }

        const ctx = this.props.record?.model?.config?.context || {};
        return ctx.default_editing_pick_ticket_id || null;
    }

    _syncCollapsedState() {
        const next = { ...this.state.collapsed };

        for (const group of this.state.groups) {
            if (!(group.productId in next)) {
                next[group.productId] = false;
            }
        }

        this.state.collapsed = next;
    }

    _writeSelectionsToRecord() {
        if (this._writeTimeout) {
            clearTimeout(this._writeTimeout);
        }

        this._writeTimeout = setTimeout(() => {
            this._doWriteSelectionsToRecord();
        }, 200);
    }

    _doWriteSelectionsToRecord() {
        const selections = [];

        for (const group of this.state.groups) {
            for (const line of group.lines) {
                if (this.state.mode === "swap") {
                    const targetLotId = parseInt(line.targetLotId || 0);

                    if (!targetLotId) continue;

                    selections.push({
                        dbId: line.dbId || 0,
                        productId: line.productId || 0,
                        productName: line.productName || "",
                        originLotId: line.originLotId || 0,
                        originLotName: line.originLotName || "",
                        targetLotId: targetLotId,
                        targetLotName: line.targetLotName || "",
                        pickingId: line.pickingId || 0,
                        moveLineId: line.moveLineId || 0,
                        saleLineId: line.saleLineId || 0,
                        qty: line.qty || 0,
                        targetQty: line.targetQty || 0,
                        targetBloque: line.targetBloque || "",
                    });

                    continue;
                }

                if (!line.isSelected) continue;

                const qty = this.state.mode === "delivery"
                    ? (line.qtyToDeliver || 0)
                    : this.state.mode === "return"
                        ? (line.qtyToReturn || 0)
                        : (line.qty || 0);

                if (qty <= 0) continue;

                selections.push({
                    dbId: line.dbId || 0,
                    lotId: line.lotId || 0,
                    productId: line.productId || 0,
                    pickingId: line.pickingId || 0,
                    moveId: line.moveId || 0,
                    moveLineId: line.moveLineId || 0,
                    saleLineId: line.saleLineId || 0,
                    sourceLocationId: line.sourceLocationId || 0,
                    qty: qty,
                    qtyAvailable: line.qtyAvailable || 0,
                });
            }
        }

        const json = JSON.stringify(selections);

        try {
            const root = this.props.record?.model?.root || this.props.record;

            if (root?.update) {
                return root.update({ widget_selections: json });
            }
        } catch (e) {
            console.warn("[DGL] Could not write widget_selections:", e?.message);
        }

        return null;
    }

    toggleGroup(productId) {
        this.state.collapsed[productId] = !this.state.collapsed[productId];
    }

    isCollapsed(productId) {
        return !!this.state.collapsed[productId];
    }

    expandAll() {
        for (const group of this.state.groups) {
            this.state.collapsed[group.productId] = false;
        }
    }

    collapseAll() {
        for (const group of this.state.groups) {
            this.state.collapsed[group.productId] = true;
        }
    }

    toggleLineSelected(lineData) {
        const newVal = !lineData.isSelected;
        lineData.isSelected = newVal;

        if (this.state.mode === "delivery") {
            lineData.qtyToDeliver = newVal ? (lineData.qtyAvailable || 0) : 0;
        } else if (this.state.mode === "return") {
            lineData.qtyToReturn = newVal ? (lineData.qtyDelivered || 0) : 0;
        }

        this._recalcGroupTotals(lineData.productId);
        this.state.groups = [...this.state.groups];
        this._writeSelectionsToRecord();
    }

    onQtyChange(lineData, event) {
        const val = parseFloat(event.target.value) || 0;

        if (this.state.mode === "delivery") {
            const max = parseFloat(lineData.qtyAvailable || 0);
            lineData.qtyToDeliver = Math.min(Math.max(val, 0), max);
            lineData.isSelected = lineData.qtyToDeliver > 0;
            event.target.value = lineData.qtyToDeliver;
        } else if (this.state.mode === "return") {
            const max = parseFloat(lineData.qtyDelivered || 0);
            lineData.qtyToReturn = Math.min(Math.max(val, 0), max);
            lineData.isSelected = lineData.qtyToReturn > 0;
            event.target.value = lineData.qtyToReturn;
        }

        this._recalcGroupTotals(lineData.productId);
        this.state.groups = [...this.state.groups];
        this._writeSelectionsToRecord();
    }

    selectAllInGroup(group) {
        if (this.state.mode === "swap") return;

        for (const line of group.lines) {
            line.isSelected = true;

            if (this.state.mode === "delivery") {
                line.qtyToDeliver = line.qtyAvailable || 0;
            } else if (this.state.mode === "return") {
                line.qtyToReturn = line.qtyDelivered || 0;
            }
        }

        this._recalcGroupTotals(group.productId);
        this.state.groups = [...this.state.groups];
        this._writeSelectionsToRecord();
    }

    deselectAllInGroup(group) {
        if (this.state.mode === "swap") return;

        for (const line of group.lines) {
            line.isSelected = false;

            if (this.state.mode === "delivery") {
                line.qtyToDeliver = 0;
            } else if (this.state.mode === "return") {
                line.qtyToReturn = 0;
            }
        }

        this._recalcGroupTotals(group.productId);
        this.state.groups = [...this.state.groups];
        this._writeSelectionsToRecord();
    }

    _recalcGroupTotals(productId) {
        const group = this.state.groups.find((g) => g.productId === productId);

        if (!group) return;

        group.totalQty = 0;
        group.selectedCount = 0;

        for (const line of group.lines) {
            if (this.state.mode === "delivery") {
                group.totalQty += line.qtyToDeliver || 0;
                if (line.isSelected) group.selectedCount += 1;
            } else if (this.state.mode === "return") {
                group.totalQty += line.qtyToReturn || 0;
                if (line.isSelected) group.selectedCount += 1;
            } else if (this.state.mode === "swap") {
                group.totalQty += line.qty || 0;
                if (line.targetLotId) group.selectedCount += 1;
            } else {
                group.totalQty += line.qty || 0;
                if (line.isSelected) group.selectedCount += 1;
            }
        }
    }

    _recalcAllGroups() {
        for (const group of this.state.groups) {
            this._recalcGroupTotals(group.productId);
        }
    }

    get totalSelectedGlobal() {
        let total = 0;

        for (const group of this.state.groups) {
            for (const line of group.lines) {
                if (this.state.mode === "delivery") {
                    if (line.isSelected) total += line.qtyToDeliver || 0;
                } else if (this.state.mode === "return") {
                    if (line.isSelected) total += line.qtyToReturn || 0;
                } else if (this.state.mode === "swap") {
                    if (line.targetLotId) total += line.qty || 0;
                }
            }
        }

        return total;
    }

    get totalAvailableGlobal() {
        let total = 0;

        for (const group of this.state.groups) {
            for (const line of group.lines) {
                if (this.state.mode === "delivery") {
                    total += line.qtyAvailable || 0;
                } else if (this.state.mode === "return") {
                    total += line.qtyDelivered || 0;
                } else if (this.state.mode === "swap") {
                    total += line.qty || 0;
                }
            }
        }

        return total;
    }

    openSwapSelector(lineData) {
        if (!lineData.productId) return;
        this._openSwapPopup(lineData, lineData.productId, lineData.originLotId);
    }

    _openSwapPopup(lineData, productId, originLotId) {
        const self = this;
        const root = document.createElement("div");
        root.className = "swap-popup-root";
        document.body.appendChild(root);

        const PAGE_SIZE = 35;

        const st = {
            quants: [],
            totalCount: 0,
            isLoading: false,
            page: 0,
            selectedLotId: lineData.targetLotId || null,
            selectedLotName: lineData.targetLotName || "",
            filters: {
                lot_name: "",
                bloque: "",
                atado: "",
            },
        };

        let searchTimeout = null;

        const cleanup = () => {
            if (root._kh) {
                document.removeEventListener("keydown", root._kh);
            }
            root.remove();
        };

        const updateUI = () => {
            const badge = root.querySelector("#dgl-sel-badge");
            const name = root.querySelector("#dgl-sel-name");
            const btns = root.querySelectorAll(".dgl-confirm-btn");

            if (st.selectedLotId) {
                badge.style.display = "";
                name.textContent = st.selectedLotName;
                btns.forEach((btn) => {
                    btn.disabled = false;
                });
            } else {
                badge.style.display = "none";
                name.textContent = "—";
                btns.forEach((btn) => {
                    btn.disabled = true;
                });
            }
        };

        const render = () => {
            const body = root.querySelector("#dgl-body");
            const stat = root.querySelector("#dgl-stat");

            if (!body || !stat) return;

            if (!st.quants.length && !st.isLoading) {
                body.innerHTML = `
                    <div class="dgl-empty">
                        <i class="fa fa-inbox fa-3x text-muted"></i>
                        <div class="mt-2">No hay lotes disponibles</div>
                    </div>
                `;
                stat.textContent = "0 lotes";
                return;
            }

            let rows = "";

            for (const q of st.quants) {
                const lotId = q.lot_id?.[0] || 0;
                const lotName = q.lot_id?.[1] || "-";

                if (lotId === originLotId) continue;

                const sel = st.selectedLotId === lotId;
                const tipo = (q.x_tipo || "placa").toLowerCase();
                const loc = q.location_id ? q.location_id[1].split("/").pop() : "-";
                const safeLotName = String(lotName).replace(/"/g, "&quot;");

                rows += `
                    <tr class="${sel ? "dgl-row-sel" : ""}" data-lid="${lotId}" data-ln="${safeLotName}">
                        <td class="text-center">
                            <div class="dgl-radio ${sel ? "checked" : ""}">
                                ${sel ? '<i class="fa fa-check"></i>' : ""}
                            </div>
                        </td>
                        <td class="dgl-cell-lot">${lotName}</td>
                        <td>${q.x_bloque || "-"}</td>
                        <td>${q.x_atado || "-"}</td>
                        <td class="text-end">${q.x_alto ? parseFloat(q.x_alto).toFixed(0) : "-"}</td>
                        <td class="text-end">${q.x_ancho ? parseFloat(q.x_ancho).toFixed(0) : "-"}</td>
                        <td class="text-end fw-bold">${q.quantity ? q.quantity.toFixed(2) : "0.00"}</td>
                        <td><span class="dgl-tag dgl-tag-${tipo}">${tipo}</span></td>
                        <td>${q.x_color || "-"}</td>
                        <td class="text-muted small">${loc}</td>
                    </tr>
                `;
            }

            body.innerHTML = `
                <table class="dgl-popup-table">
                    <thead>
                        <tr>
                            <th style="width:36px"></th>
                            <th>Lote</th>
                            <th>Bloque</th>
                            <th>Atado</th>
                            <th class="text-end">Alto</th>
                            <th class="text-end">Ancho</th>
                            <th class="text-end">m²</th>
                            <th>Tipo</th>
                            <th>Color</th>
                            <th>Ubic.</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
            `;

            stat.textContent = `${st.totalCount} lotes`;

            body.querySelectorAll("tr[data-lid]").forEach((tr) => {
                tr.style.cursor = "pointer";
                tr.addEventListener("click", () => {
                    const id = parseInt(tr.dataset.lid);

                    if (st.selectedLotId === id) {
                        st.selectedLotId = null;
                        st.selectedLotName = "";
                    } else {
                        st.selectedLotId = id;
                        st.selectedLotName = tr.dataset.ln;
                    }

                    updateUI();
                    render();
                });
            });
        };

        const load = async (page, reset) => {
            if (reset) {
                st.quants = [];
            }

            st.isLoading = true;

            try {
                let result;

                try {
                    result = await self.orm.call(
                        "stock.quant",
                        "search_stone_inventory_for_so_paginated",
                        [],
                        {
                            product_id: productId,
                            filters: st.filters,
                            current_lot_ids: [],
                            page,
                            page_size: PAGE_SIZE,
                        }
                    );
                } catch (_e) {
                    const all = await self.orm.call(
                        "stock.quant",
                        "search_stone_inventory_for_so",
                        [],
                        {
                            product_id: productId,
                            filters: st.filters,
                            current_lot_ids: [],
                        }
                    ) || [];

                    result = {
                        items: all.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE),
                        total: all.length,
                    };
                }

                st.quants = reset
                    ? (result.items || [])
                    : [...st.quants, ...(result.items || [])];

                st.totalCount = result.total || 0;
                st.page = page;
            } catch (e) {
                console.error("[DGL SWAP]", e);
            } finally {
                st.isLoading = false;
                render();
            }
        };

        const doConfirm = async () => {
            if (!st.selectedLotId) return;

            const selectedQuant = (st.quants || []).find((q) => {
                const lotId = q.lot_id?.[0] || 0;
                return lotId === st.selectedLotId;
            });

            lineData.targetLotId = st.selectedLotId;
            lineData.targetLotName = st.selectedLotName || "";
            lineData.targetBloque = selectedQuant?.x_bloque || "";
            lineData.targetQty = selectedQuant?.quantity || 0;

            self._recalcGroupTotals(lineData.productId);
            self.state.groups = [...self.state.groups];

            await self._doWriteSelectionsToRecord();

            cleanup();

            if (lineData.dbId) {
                try {
                    await self.orm.write(self._lineModel, [lineData.dbId], {
                        target_lot_id: st.selectedLotId,
                    });
                } catch (e) {
                    console.warn(
                        "[DGL SWAP] No se pudo escribir target_lot_id en la línea transitoria. Se usará widget_selections.",
                        e
                    );
                }
            }
        };

        root.innerHTML = `
            <div class="dgl-overlay" id="dgl-overlay">
                <div class="dgl-popup">
                    <div class="dgl-popup-header">
                        <span>
                            <i class="fa fa-exchange me-2"></i>
                            Seleccionar Lote de Reemplazo
                        </span>

                        <div class="d-flex align-items-center gap-2">
                            <span class="dgl-origin-badge">
                                <i class="fa fa-cube me-1"></i>
                                Actual: <strong>${lineData.originLotName || ""}</strong>
                            </span>

                            <span class="dgl-sel-badge" id="dgl-sel-badge" style="display:none">
                                <i class="fa fa-arrow-right me-1"></i>
                                Nuevo: <strong id="dgl-sel-name">—</strong>
                            </span>

                            <button class="dgl-confirm-btn dgl-btn-green" disabled>
                                <i class="fa fa-check me-1"></i>
                                Confirmar
                            </button>

                            <button class="dgl-close-btn">
                                <i class="fa fa-times"></i>
                            </button>
                        </div>
                    </div>

                    <div class="dgl-popup-filters">
                        <div class="dgl-fg">
                            <label>Lote</label>
                            <input type="text" id="dglf-lot" placeholder="Buscar..."/>
                        </div>

                        <div class="dgl-fg">
                            <label>Bloque</label>
                            <input type="text" id="dglf-bloque" placeholder="Bloque..."/>
                        </div>

                        <div class="dgl-fg">
                            <label>Atado</label>
                            <input type="text" id="dglf-atado" placeholder="Atado..."/>
                        </div>

                        <div class="dgl-spacer"></div>
                        <span id="dgl-stat" class="text-muted small">Buscando...</span>
                    </div>

                    <div class="dgl-popup-body" id="dgl-body">
                        <div class="dgl-empty">
                            <i class="fa fa-circle-o-notch fa-spin fa-2x text-muted"></i>
                        </div>
                    </div>

                    <div class="dgl-popup-footer">
                        <button class="dgl-btn-outline" id="dgl-cancel">Cancelar</button>

                        <button class="dgl-confirm-btn dgl-btn-primary" disabled>
                            <i class="fa fa-exchange me-1"></i>
                            Usar este lote
                        </button>
                    </div>
                </div>
            </div>
        `;

        root.querySelector(".dgl-close-btn").addEventListener("click", cleanup);
        root.querySelector("#dgl-cancel").addEventListener("click", cleanup);

        root.querySelector("#dgl-overlay").addEventListener("click", (e) => {
            if (e.target.id === "dgl-overlay") {
                cleanup();
            }
        });

        root.querySelectorAll(".dgl-confirm-btn").forEach((button) => {
            button.addEventListener("click", doConfirm);
        });

        root._kh = (e) => {
            if (e.key === "Escape") {
                cleanup();
            }
        };

        document.addEventListener("keydown", root._kh);

        [
            "dglf-lot:lot_name",
            "dglf-bloque:bloque",
            "dglf-atado:atado",
        ].forEach((pair) => {
            const [id, key] = pair.split(":");
            const el = root.querySelector(`#${id}`);

            if (el) {
                el.addEventListener("input", () => {
                    st.filters[key] = el.value;

                    if (searchTimeout) {
                        clearTimeout(searchTimeout);
                    }

                    searchTimeout = setTimeout(() => load(0, true), 350);
                });
            }
        });

        if (st.selectedLotId) {
            updateUI();
        }

        load(0, true);
    }

    async clearSwapTarget(lineData) {
        lineData.targetLotId = 0;
        lineData.targetLotName = "";
        lineData.targetBloque = "";
        lineData.targetQty = 0;

        this._recalcGroupTotals(lineData.productId);
        this.state.groups = [...this.state.groups];

        await this._doWriteSelectionsToRecord();

        if (lineData.dbId) {
            try {
                await this.orm.write(this._lineModel, [lineData.dbId], {
                    target_lot_id: false,
                });
            } catch (e) {
                console.warn(
                    "[DGL SWAP] No se pudo limpiar target_lot_id en DB. Se limpió widget_selections.",
                    e
                );
            }
        }
    }

    fmt(num) {
        if (num === null || num === undefined || isNaN(num)) return "0.00";
        return parseFloat(num).toFixed(2);
    }

    fmtDim(val) {
        if (!val) return "-";

        const v = parseFloat(val);

        if (isNaN(v)) return "-";

        return v % 1 === 0 ? v.toFixed(0) : v.toFixed(2);
    }
}

registry.category("fields").add("delivery_grouped_list", {
    component: DeliveryGroupedList,
    displayName: "Delivery Grouped List",
    supportedTypes: ["one2many"],
});