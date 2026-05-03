# -*- coding: utf-8 -*-

import logging

from odoo import models

_logger = logging.getLogger(__name__)


class SaleSwapWizardHistoryHook(models.TransientModel):
    """
    Hook de integración entre sale_delivery_wizard y sale_stone_selection.

    Este hook registra cada swap exitoso en sale.stone.swap.history.

    Debe vivir aquí, en sale_delivery_wizard, porque:
    - sale.swap.wizard se define en sale_delivery_wizard.
    - sale.stone.swap.history se define en sale_stone_selection.
    - sale_delivery_wizard ya depende de sale_stone_selection.
    - sale_stone_selection NO debe depender de sale_delivery_wizard.
    """
    _inherit = 'sale.swap.wizard'

    def action_confirm_swap(self):
        try:
            pairs = self._collect_swap_pairs_for_history()
        except Exception as exc:
            _logger.warning(
                "[STONE SWAP HISTORY] No se pudieron recolectar pares antes del swap: %s",
                exc,
            )
            pairs = []

        result = super().action_confirm_swap()

        if pairs and 'sale.stone.swap.history' in self.env:
            History = self.env['sale.stone.swap.history']

            for vals in pairs:
                try:
                    History.create(vals)
                except Exception as exc:
                    _logger.warning(
                        "[STONE SWAP HISTORY] Error registrando swap %s: %s",
                        vals,
                        exc,
                    )

        return result

    def _collect_swap_pairs_for_history(self):
        self.ensure_one()

        pairs = []
        lines = []

        if hasattr(self, '_get_swap_lines_from_widget_selections'):
            try:
                lines = self._get_swap_lines_from_widget_selections()
            except Exception as exc:
                _logger.warning(
                    "[STONE SWAP HISTORY] Error leyendo widget_selections: %s",
                    exc,
                )
                lines = []

        if not lines and hasattr(self, '_get_swap_lines_from_db_lines'):
            try:
                lines = self._get_swap_lines_from_db_lines()
            except Exception as exc:
                _logger.warning(
                    "[STONE SWAP HISTORY] Error leyendo líneas DB: %s",
                    exc,
                )
                lines = []

        for data in lines or []:
            move_line = data.get('move_line')
            old_lot = data.get('origin_lot')
            new_lot = data.get('target_lot')

            if not move_line or not old_lot or not new_lot:
                continue

            if old_lot.id == new_lot.id:
                continue

            sale_line = data.get('sale_line')
            if not sale_line and move_line.move_id:
                sale_line = move_line.move_id.sale_line_id

            if not sale_line:
                continue

            pairs.append({
                'sale_line_id': sale_line.id,
                'old_lot_id': old_lot.id,
                'new_lot_id': new_lot.id,
                'old_qty': data.get('qty', 0.0) or 0.0,
                'new_qty': (
                    data.get('target_qty', 0.0)
                    or data.get('qty', 0.0)
                    or 0.0
                ),
            })

        return pairs