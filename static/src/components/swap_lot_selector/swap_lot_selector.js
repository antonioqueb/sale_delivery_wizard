/** @odoo-module */
import { registry } from "@web/core/registry";

/**
 * Client action: swap_open_lot_picker
 *
 * Opens a fullscreen popup to select a replacement lot.
 * On confirm, writes directly via RPC to the wizard line (ORM write),
 * then reopens the swap wizard so the user sees the updated state.
 *
 * This approach bypasses the OWL dirty-tracking problem entirely:
 * - Python button on line → returns this client action
 * - JS handles the popup
 * - JS calls action_write_target_lot via RPC → ORM write
 * - JS reopens wizard via doAction
 */
function swapOpenLotPicker(env, action) {
    const params = action.params || {};
    const orm = env.services.orm;
    const actionService = env.services.action;

    const {
        wizard_id,
        line_id,
        product_id,
        product_name,
        origin_lot_id,
        origin_lot_name,
        current_target_lot_id,
        current_target_lot_name,
        exclude_lot_ids,
    } = params;

    if (!product_id || !line_id) {
        console.warn("[SWAP] Missing params");
        return;
    }

    const popupRoot = document.createElement("div");
    popupRoot.className = "swap-popup-root";
    document.body.appendChild(popupRoot);

    const PAGE_SIZE = 35;
    const excludeSet = new Set(exclude_lot_ids || []);

    const state = {
        quants: [],
        totalCount: 0,
        hasMore: false,
        isLoading: false,
        isLoadingMore: false,
        page: 0,
        selectedLotId: current_target_lot_id || null,
        selectedLotName: current_target_lot_name || "",
        filters: { lot_name: "", bloque: "", atado: "", alto_min: "", ancho_min: "", tipo: "" },
    };

    let searchTimeout = null;
    let popupObserver = null;
    let popupKeyHandler = null;

    function destroyPopup() {
        if (popupObserver) { popupObserver.disconnect(); popupObserver = null; }
        if (popupKeyHandler) { document.removeEventListener("keydown", popupKeyHandler); popupKeyHandler = null; }
        if (popupRoot.parentNode) { popupRoot.remove(); }
    }

    // ─── Build popup HTML ────────────────────────────────────────────────
    popupRoot.innerHTML = `
        <div class="swap-popup-overlay" id="swap-overlay">
            <div class="swap-popup-container">
                <div class="swap-popup-header">
                    <div class="swap-popup-title">
                        <i class="fa fa-exchange me-2"></i>
                        Seleccionar Lote de Reemplazo
                        <span class="swap-popup-subtitle">${product_name ? "— " + product_name : ""}</span>
                    </div>
                    <div class="swap-popup-header-actions">
                        <div class="swap-origin-badge">
                            <i class="fa fa-cube me-1"></i>
                            Actual: <strong>${origin_lot_name || "—"}</strong>
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

    const overlay = popupRoot.querySelector("#swap-overlay");
    const body = popupRoot.querySelector("#swap-body");
    const stat = popupRoot.querySelector("#swap-stat");
    const footerInfo = popupRoot.querySelector("#swap-footer-info");
    const selBadge = popupRoot.querySelector("#swap-sel-badge");
    const selName = popupRoot.querySelector("#swap-sel-name");
    const confirmTop = popupRoot.querySelector("#swap-confirm-top");
    const confirmBottom = popupRoot.querySelector("#swap-confirm-bottom");

    function updateSelection(lotId, lotName) {
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
    }

    function updateStats() {
        stat.className = "swap-stat-count";
        stat.innerHTML = `${state.totalCount} lotes disponibles`;
        footerInfo.innerHTML = `Mostrando <strong>${state.quants.length}</strong> de <strong>${state.totalCount}</strong>`;
    }

    function esc(s) {
        return String(s || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#39;").replace(/</g, "&lt;");
    }

    function renderTable() {
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
            // Exclude lots already in the picking (all origin lots)
            if (excludeSet.has(lotId)) continue;

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

            rows += `
                <tr class="${sel ? "swap-row-sel" : ""}" data-lot-id="${lotId}" data-lot-name="${esc(lotName)}">
                    <td class="col-chk">
                        <div class="swap-radio ${sel ? "checked" : ""}">
                            ${sel ? '<i class="fa fa-check"></i>' : ""}
                        </div>
                    </td>
                    <td class="swap-cell-lot">${esc(lotName)}</td>
                    <td>${esc(q.x_bloque) || "-"}</td>
                    <td>${esc(q.x_atado) || "-"}</td>
                    <td class="col-num">${q.x_alto ? parseFloat(q.x_alto).toFixed(0) : "-"}</td>
                    <td class="col-num">${q.x_ancho ? parseFloat(q.x_ancho).toFixed(0) : "-"}</td>
                    <td class="col-num">${q.x_grosor || "-"}</td>
                    <td class="col-num fw-semibold">${area}</td>
                    <td><span class="swap-tag swap-tag-tipo-${tipo}">${tipoLabel}</span></td>
                    <td>${esc(q.x_color) || "-"}</td>
                    <td>${esc(q.x_origen) || "-"}</td>
                    <td class="swap-cell-loc">${esc(loc)}</td>
                    <td class="col-num font-monospace text-muted">${esc(q.x_pedimento) || "-"}</td>
                    <td>${q.x_detalles_placa
                        ? `<i class="fa fa-info-circle text-warning" title="${esc(q.x_detalles_placa)}"></i>`
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
                const lid = parseInt(tr.dataset.lotId);
                const lname = tr.dataset.lotName;
                if (!lid) return;
                if (state.selectedLotId === lid) {
                    updateSelection(null, "");
                } else {
                    updateSelection(lid, lname);
                }
                renderTable();
            });
        });

        // Infinite scroll
        if (popupObserver) { popupObserver.disconnect(); popupObserver = null; }
        const sentinelEl = body.querySelector("#swap-sentinel");
        if (sentinelEl && state.hasMore) {
            popupObserver = new IntersectionObserver(
                (entries) => {
                    if (entries[0].isIntersecting && state.hasMore && !state.isLoadingMore) {
                        loadPage(state.page + 1, false);
                    }
                },
                { root: body, rootMargin: "100px", threshold: 0.1 }
            );
            popupObserver.observe(sentinelEl);
        }
    }

    async function loadPage(page, reset) {
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
                result = await orm.call(
                    "stock.quant",
                    "search_stone_inventory_for_so_paginated",
                    [],
                    { product_id, filters: state.filters, current_lot_ids: [], page, page_size: PAGE_SIZE }
                );
            } catch (_e) {
                const all = (await orm.call(
                    "stock.quant", "search_stone_inventory_for_so",
                    [], { product_id, filters: state.filters, current_lot_ids: [] }
                )) || [];
                result = { items: all.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE), total: all.length };
            }

            const items = result.items || [];
            if (reset || page === 0) { state.quants = items; }
            else { state.quants = [...state.quants, ...items]; }
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
    }

    async function doConfirm() {
        if (!state.selectedLotId) return;
        destroyPopup();

        try {
            // Write target_lot_id directly to the wizard line via ORM
            await orm.call(
                "sale.swap.wizard.line",
                "action_write_target_lot",
                [line_id, state.selectedLotId]
            );
            console.log("[SWAP] Persisted lot %s on line %s via RPC", state.selectedLotId, line_id);

            // Reopen the wizard to reflect the change
            await actionService.doAction({
                type: 'ir.actions.act_window',
                res_model: 'sale.swap.wizard',
                res_id: wizard_id,
                view_mode: 'form',
                views: [[false, 'form']],
                target: 'new',
            });
        } catch (e) {
            console.error("[SWAP] Error:", e);
        }
    }

    function doClose() {
        destroyPopup();
        // Reopen wizard so user returns to where they were
        actionService.doAction({
            type: 'ir.actions.act_window',
            res_model: 'sale.swap.wizard',
            res_id: wizard_id,
            view_mode: 'form',
            views: [[false, 'form']],
            target: 'new',
        });
    }

    // ─── Events ──────────────────────────────────────────────────────────
    popupRoot.querySelector("#swap-close").addEventListener("click", doClose);
    popupRoot.querySelector("#swap-cancel").addEventListener("click", doClose);
    popupRoot.querySelector("#swap-confirm-top").addEventListener("click", doConfirm);
    popupRoot.querySelector("#swap-confirm-bottom").addEventListener("click", doConfirm);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) doClose(); });

    popupKeyHandler = (e) => { if (e.key === "Escape") doClose(); };
    document.addEventListener("keydown", popupKeyHandler);

    const bindFilter = (id, key) => {
        const input = popupRoot.querySelector(`#${id}`);
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

registry.category("actions").add("swap_open_lot_picker", swapOpenLotPicker);