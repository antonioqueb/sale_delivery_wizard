/** @odoo-module */
import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, useState, onWillStart, onWillUpdateProps, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

export class SwapLotSelector extends Component {
    static template = "sale_delivery_wizard.SwapLotSelector";
    static props = { ...standardFieldProps };

    setup() {
        this.orm = useService("orm");
        this._popupRoot = null;
        this._popupKeyHandler = null;
        this._popupObserver = null;

        this.state = useState({
            targetLotName: "",
            targetLotId: null,
        });

        onWillStart(() => {
            this._syncFromRecord();
        });

        onWillUpdateProps((nextProps) => {
            this._syncFromRecordWith(nextProps);
        });

        onWillUnmount(() => {
            this.destroyPopup();
        });
    }

    _syncFromRecord() {
        this._syncFromRecordWith(this.props);
    }

    _syncFromRecordWith(props) {
        const val = props.record.data[props.name];
        if (val) {
            if (Array.isArray(val)) {
                this.state.targetLotId = val[0] || null;
                this.state.targetLotName = val[1] || "";
            } else if (typeof val === "object" && val.id) {
                this.state.targetLotId = val.id;
                this.state.targetLotName = val.display_name || val.name || "";
            } else if (typeof val === "number") {
                this.state.targetLotId = val;
                this.state.targetLotName = "";
            } else {
                this.state.targetLotId = null;
                this.state.targetLotName = "";
            }
        } else {
            this.state.targetLotId = null;
            this.state.targetLotName = "";
        }
    }

    _getProductId() {
        const pd = this.props.record.data.product_id;
        if (!pd) return 0;
        if (Array.isArray(pd)) return pd[0];
        if (typeof pd === "number") return pd;
        if (pd && pd.id) return pd.id;
        return 0;
    }

    _getOriginLotId() {
        const lot = this.props.record.data.origin_lot_id;
        if (!lot) return 0;
        if (Array.isArray(lot)) return lot[0];
        if (typeof lot === "number") return lot;
        if (lot && lot.id) return lot.id;
        return 0;
    }

    _getOriginLotName() {
        const lot = this.props.record.data.origin_lot_id;
        if (!lot) return "";
        if (Array.isArray(lot)) return lot[1] || "";
        if (lot && lot.display_name) return lot.display_name;
        if (lot && lot.name) return lot.name;
        return "";
    }

    _getProductName() {
        const pd = this.props.record.data.product_id;
        if (!pd) return "";
        if (Array.isArray(pd)) return pd[1] || "";
        if (pd && pd.display_name) return pd.display_name;
        return "";
    }

    // ─── Check if truly readonly ──────────────────────────────────────────
    // In Odoo 19, props.readonly may be forced true by parent context.
    // We check the field definition on the record to see if it's truly readonly.
    get isEditable() {
        // Always editable — the view controls this, not JS
        return true;
    }

    handleClick(ev) {
        ev.stopPropagation();
        ev.preventDefault();
        this.openPopup();
    }

    async handleClear(ev) {
        ev.stopPropagation();
        ev.preventDefault();
        this.state.targetLotId = null;
        this.state.targetLotName = "";
        try {
            await this.props.record.update({ [this.props.name]: false });
        } catch (e) {
            console.warn("[SWAP] Error clearing lot:", e);
        }
    }

    // ═══════════════════════════════════════════════════════════════════════════
    // POPUP
    // ═══════════════════════════════════════════════════════════════════════════

    openPopup() {
        this.destroyPopup();
        const productId = this._getProductId();
        if (!productId) {
            console.warn("[SWAP] No product_id found, cannot open popup");
            return;
        }

        this._popupRoot = document.createElement("div");
        this._popupRoot.className = "swap-popup-root";
        document.body.appendChild(this._popupRoot);

        this._renderPopup(productId);
    }

    _renderPopup(productId) {
        const root = this._popupRoot;
        const PAGE_SIZE = 35;
        const originLotId = this._getOriginLotId();

        const state = {
            quants: [],
            totalCount: 0,
            hasMore: false,
            isLoading: false,
            isLoadingMore: false,
            page: 0,
            selectedLotId: this.state.targetLotId,
            selectedLotName: this.state.targetLotName,
            filters: { lot_name: "", bloque: "", atado: "", alto_min: "", ancho_min: "", tipo: "" },
        };

        let searchTimeout = null;

        root.innerHTML = `
            <div class="swap-popup-overlay" id="swap-overlay">
                <div class="swap-popup-container">
                    <div class="swap-popup-header">
                        <div class="swap-popup-title">
                            <i class="fa fa-exchange me-2"></i>
                            Seleccionar Lote de Reemplazo
                            <span class="swap-popup-subtitle">${this._getProductName() ? "— " + this._getProductName() : ""}</span>
                        </div>
                        <div class="swap-popup-header-actions">
                            <div class="swap-origin-badge">
                                <i class="fa fa-cube me-1"></i>
                                Actual: <strong>${this._getOriginLotName()}</strong>
                            </div>
                            <div class="swap-selected-badge" id="swap-sel-badge" style="display:none;">
                                <i class="fa fa-arrow-right me-1"></i>
                                Nuevo: <strong id="swap-sel-name">—</strong>
                            </div>
                            <button class="swap-btn swap-btn-confirm" id="swap-confirm-top" disabled>
                                <i class="fa fa-check me-1"></i> Confirmar
                            </button>
                            <button class="swap-btn swap-btn-ghost" id="swap-close">
                                <i class="fa fa-times"></i>
                            </button>
                        </div>
                    </div>

                    <div class="swap-popup-filters">
                        <div class="swap-filter-group">
                            <label>Lote</label>
                            <input type="text" class="swap-filter-input" id="swf-lot" placeholder="Buscar lote..."/>
                        </div>
                        <div class="swap-filter-group">
                            <label>Bloque</label>
                            <input type="text" class="swap-filter-input" id="swf-bloque" placeholder="Bloque..."/>
                        </div>
                        <div class="swap-filter-group">
                            <label>Atado</label>
                            <input type="text" class="swap-filter-input" id="swf-atado" placeholder="Atado..."/>
                        </div>
                        <div class="swap-filter-group">
                            <label>Alto mín.</label>
                            <input type="number" class="swap-filter-input swap-filter-sm" id="swf-alto" placeholder="0"/>
                        </div>
                        <div class="swap-filter-group">
                            <label>Ancho mín.</label>
                            <input type="number" class="swap-filter-input swap-filter-sm" id="swf-ancho" placeholder="0"/>
                        </div>
                        <div class="swap-filter-group">
                            <label>Tipo</label>
                            <select class="swap-filter-input" id="swf-tipo">
                                <option value="">Todos</option>
                                <option value="placa">Placa</option>
                                <option value="formato">Formato</option>
                                <option value="pieza">Pieza</option>
                            </select>
                        </div>
                        <div class="swap-filter-spacer"></div>
                        <div class="swap-filter-stats">
                            <span id="swap-stat" class="swap-stat-loading">
                                <i class="fa fa-circle-o-notch fa-spin me-1"></i> Buscando...
                            </span>
                        </div>
                    </div>

                    <div class="swap-popup-body" id="swap-body">
                        <div class="swap-empty-state">
                            <i class="fa fa-circle-o-notch fa-spin fa-2x text-muted"></i>
                            <div class="swap-empty-text mt-2">Cargando inventario...</div>
                        </div>
                    </div>

                    <div class="swap-popup-footer">
                        <span class="swap-footer-info" id="swap-footer-info">—</span>
                        <div class="swap-footer-actions">
                            <button class="swap-btn swap-btn-outline" id="swap-cancel">Cancelar</button>
                            <button class="swap-btn swap-btn-primary" id="swap-confirm-bottom" disabled>
                                <i class="fa fa-exchange me-1"></i> Usar este lote
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        `;

        const overlay = root.querySelector("#swap-overlay");
        const body = root.querySelector("#swap-body");
        const stat = root.querySelector("#swap-stat");
        const footerInfo = root.querySelector("#swap-footer-info");
        const selBadge = root.querySelector("#swap-sel-badge");
        const selName = root.querySelector("#swap-sel-name");
        const confirmTop = root.querySelector("#swap-confirm-top");
        const confirmBottom = root.querySelector("#swap-confirm-bottom");

        const updateSelection = (lotId, lotName) => {
            state.selectedLotId = lotId;
            state.selectedLotName = lotName;
            if (lotId) {
                selBadge.style.display = "";
                selName.textContent = lotName;
                confirmTop.disabled = false;
                confirmBottom.disabled = false;
            } else {
                selBadge.style.display = "none";
                selName.textContent = "—";
                confirmTop.disabled = true;
                confirmBottom.disabled = true;
            }
        };

        const updateStats = () => {
            stat.className = "swap-stat-count";
            stat.innerHTML = `${state.totalCount} lotes disponibles`;
            footerInfo.innerHTML = `Mostrando <strong>${state.quants.length}</strong> de <strong>${state.totalCount}</strong>`;
        };

        const renderTable = () => {
            if (state.quants.length === 0 && !state.isLoading) {
                body.innerHTML = `
                    <div class="swap-empty-state">
                        <i class="fa fa-inbox fa-3x text-muted"></i>
                        <div class="swap-empty-text mt-2">No hay lotes disponibles con estos filtros</div>
                    </div>`;
                updateStats();
                return;
            }

            let rows = "";
            for (const q of state.quants) {
                const lotId = q.lot_id ? q.lot_id[0] : 0;
                const lotName = q.lot_id ? q.lot_id[1] : "-";
                if (lotId === originLotId) continue;

                const loc = q.location_id ? q.location_id[1].split("/").pop() : "-";
                const sel = state.selectedLotId === lotId;
                const reserved = q.reserved_quantity > 0;
                const tipo = (q.x_tipo || "placa").toLowerCase();
                const tipoLabel = tipo.charAt(0).toUpperCase() + tipo.slice(1);
                const area = q.quantity ? q.quantity.toFixed(2) : "0.00";

                let statusBadge;
                if (sel) {
                    statusBadge = `<span class="swap-tag swap-tag-selected"><i class="fa fa-check me-1"></i>Seleccionado</span>`;
                } else if (reserved) {
                    statusBadge = `<span class="swap-tag swap-tag-warn">Reservado</span>`;
                } else {
                    statusBadge = `<span class="swap-tag swap-tag-free">Disponible</span>`;
                }

                const escapedName = lotName.replace(/"/g, '&quot;').replace(/'/g, '&#39;');

                rows += `
                    <tr class="${sel ? "swap-row-sel" : ""}" data-lot-id="${lotId}" data-lot-name="${escapedName}">
                        <td class="col-chk">
                            <div class="swap-radio ${sel ? "checked" : ""}">
                                ${sel ? '<i class="fa fa-check"></i>' : ""}
                            </div>
                        </td>
                        <td class="swap-cell-lot">${lotName}</td>
                        <td>${q.x_bloque || "-"}</td>
                        <td>${q.x_atado || "-"}</td>
                        <td class="col-num">${q.x_alto ? parseFloat(q.x_alto).toFixed(0) : "-"}</td>
                        <td class="col-num">${q.x_ancho ? parseFloat(q.x_ancho).toFixed(0) : "-"}</td>
                        <td class="col-num">${q.x_grosor || "-"}</td>
                        <td class="col-num fw-semibold">${area}</td>
                        <td><span class="swap-tag swap-tag-tipo-${tipo}">${tipoLabel}</span></td>
                        <td>${q.x_color || "-"}</td>
                        <td>${q.x_origen || "-"}</td>
                        <td class="swap-cell-loc">${loc}</td>
                        <td class="col-num font-monospace text-muted">${q.x_pedimento || "-"}</td>
                        <td>${q.x_detalles_placa
                            ? `<i class="fa fa-info-circle text-warning" title="${q.x_detalles_placa.replace(/"/g, '&quot;')}"></i>`
                            : "-"}</td>
                        <td>${statusBadge}</td>
                    </tr>`;
            }

            const sentinel = `
                <div id="swap-sentinel" class="swap-scroll-sentinel">
                    ${state.isLoadingMore ? '<div class="swap-loading-more"><i class="fa fa-circle-o-notch fa-spin me-2"></i> Cargando más...</div>' : ""}
                    ${state.hasMore && !state.isLoadingMore ? '<div class="swap-scroll-hint"><i class="fa fa-chevron-down me-1"></i> Desplázate para más</div>' : ""}
                </div>`;

            body.innerHTML = `
                <table class="swap-popup-table">
                    <thead>
                        <tr>
                            <th class="col-chk" style="width:40px;"></th>
                            <th>Lote</th>
                            <th>Bloque</th>
                            <th>Atado</th>
                            <th class="col-num">Alto</th>
                            <th class="col-num">Ancho</th>
                            <th class="col-num">Gros.</th>
                            <th class="col-num">m²</th>
                            <th>Tipo</th>
                            <th>Color</th>
                            <th>Origen</th>
                            <th>Ubic.</th>
                            <th class="col-num">Pedimento</th>
                            <th>Notas</th>
                            <th>Estado</th>
                        </tr>
                    </thead>
                    <tbody>${rows}</tbody>
                </table>
                ${sentinel}`;

            updateStats();

            body.querySelectorAll("tr[data-lot-id]").forEach((tr) => {
                tr.style.cursor = "pointer";
                tr.addEventListener("click", () => {
                    const lotId = parseInt(tr.dataset.lotId);
                    const lotName = tr.dataset.lotName;
                    if (!lotId) return;
                    if (state.selectedLotId === lotId) {
                        updateSelection(null, "");
                    } else {
                        updateSelection(lotId, lotName);
                    }
                    renderTable();
                });
            });

            if (this._popupObserver) {
                this._popupObserver.disconnect();
                this._popupObserver = null;
            }
            const sentinelEl = body.querySelector("#swap-sentinel");
            if (sentinelEl && state.hasMore) {
                this._popupObserver = new IntersectionObserver(
                    (entries) => {
                        if (entries[0].isIntersecting && state.hasMore && !state.isLoadingMore) {
                            loadPage(state.page + 1, false);
                        }
                    },
                    { root: body, rootMargin: "100px", threshold: 0.1 }
                );
                this._popupObserver.observe(sentinelEl);
            }
        };

        const loadPage = async (page, reset) => {
            if (reset) {
                state.isLoading = true;
                state.quants = [];
                body.innerHTML = `
                    <div class="swap-empty-state">
                        <i class="fa fa-circle-o-notch fa-spin fa-2x text-muted"></i>
                        <div class="swap-empty-text mt-2">Buscando...</div>
                    </div>`;
                stat.className = "swap-stat-loading";
                stat.innerHTML = `<i class="fa fa-circle-o-notch fa-spin me-1"></i> Buscando...`;
            } else {
                state.isLoadingMore = true;
            }

            try {
                let result;
                try {
                    result = await this.orm.call(
                        "stock.quant",
                        "search_stone_inventory_for_so_paginated",
                        [],
                        {
                            product_id: productId,
                            filters: state.filters,
                            current_lot_ids: originLotId ? [originLotId] : [],
                            page,
                            page_size: PAGE_SIZE,
                        }
                    );
                } catch (_e) {
                    const all = (await this.orm.call(
                        "stock.quant",
                        "search_stone_inventory_for_so",
                        [],
                        {
                            product_id: productId,
                            filters: state.filters,
                            current_lot_ids: originLotId ? [originLotId] : [],
                        }
                    )) || [];
                    result = {
                        items: all.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE),
                        total: all.length,
                    };
                }

                const items = result.items || [];
                if (reset || page === 0) {
                    state.quants = items;
                } else {
                    state.quants = [...state.quants, ...items];
                }
                state.totalCount = result.total || 0;
                state.page = page;
                state.hasMore = state.quants.length < state.totalCount;
            } catch (err) {
                console.error("[SWAP POPUP] Error:", err);
                body.innerHTML = `
                    <div class="swap-empty-state">
                        <i class="fa fa-exclamation-triangle fa-2x text-danger"></i>
                        <div class="swap-empty-text mt-2 text-danger">Error: ${err.message}</div>
                    </div>`;
                return;
            } finally {
                state.isLoading = false;
                state.isLoadingMore = false;
            }

            renderTable();
        };

        const doConfirm = async () => {
            if (!state.selectedLotId) return;
            this.state.targetLotId = state.selectedLotId;
            this.state.targetLotName = state.selectedLotName;
            this.destroyPopup();
            try {
                await this.props.record.update({
                    [this.props.name]: state.selectedLotId,
                });
            } catch (e) {
                console.error("[SWAP] Error updating record:", e);
            }
        };

        const doClose = () => this.destroyPopup();

        root.querySelector("#swap-close").addEventListener("click", doClose);
        root.querySelector("#swap-cancel").addEventListener("click", doClose);
        root.querySelector("#swap-confirm-top").addEventListener("click", doConfirm);
        root.querySelector("#swap-confirm-bottom").addEventListener("click", doConfirm);
        overlay.addEventListener("click", (e) => { if (e.target === overlay) doClose(); });

        const onKeyDown = (e) => { if (e.key === "Escape") doClose(); };
        document.addEventListener("keydown", onKeyDown);
        this._popupKeyHandler = onKeyDown;

        const bindFilter = (id, key) => {
            const input = root.querySelector(`#${id}`);
            if (!input) return;
            const handler = () => {
                state.filters[key] = input.value;
                if (searchTimeout) clearTimeout(searchTimeout);
                searchTimeout = setTimeout(() => loadPage(0, true), 350);
            };
            input.addEventListener("input", handler);
            input.addEventListener("change", handler);
        };
        bindFilter("swf-lot", "lot_name");
        bindFilter("swf-bloque", "bloque");
        bindFilter("swf-atado", "atado");
        bindFilter("swf-alto", "alto_min");
        bindFilter("swf-ancho", "ancho_min");
        bindFilter("swf-tipo", "tipo");

        if (state.selectedLotId) {
            updateSelection(state.selectedLotId, state.selectedLotName);
        }

        loadPage(0, true);
    }

    destroyPopup() {
        if (this._popupObserver) {
            this._popupObserver.disconnect();
            this._popupObserver = null;
        }
        if (this._popupKeyHandler) {
            document.removeEventListener("keydown", this._popupKeyHandler);
            this._popupKeyHandler = null;
        }
        if (this._popupRoot) {
            this._popupRoot.remove();
            this._popupRoot = null;
        }
    }
}

registry.category("fields").add("swap_lot_selector", {
    component: SwapLotSelector,
    displayName: "Swap Lot Selector",
    supportedTypes: ["many2one"],
});