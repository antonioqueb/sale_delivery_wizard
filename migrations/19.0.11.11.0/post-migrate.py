# -*- coding: utf-8 -*-
"""19.0.11.11.0 — Barrido de pickings de regeneración duplicados.

No se corre aquí: en la migración de este módulo todavía no está cargado
stock_whole_lot_removal (estrategia de remoción whole_lot_partial) y
cancelar un picking reservado truena. Se programa el cron de barrido para
que corra en cuanto termine la actualización, con el registro completo.
"""
import logging
from datetime import datetime, timedelta

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    cron = env.ref('sale_delivery_wizard.cron_som_regen_sweep', raise_if_not_found=False)
    if cron:
        cron.sudo().write({'nextcall': datetime.now() + timedelta(minutes=2), 'active': True})
        _logger.info('[sale_delivery_wizard 11.11.0] barrido de regeneración programado (cron en 2 min)')
