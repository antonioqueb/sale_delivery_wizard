# -*- coding: utf-8 -*-
"""19.0.11.11.1 — Reprograma el cron de barrido (para bases que ya pasaron
por 11.11.0 con el barrido dentro de la migración, que fallaba en pickings
reservados)."""
import logging
from datetime import datetime, timedelta

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    cron = env.ref('sale_delivery_wizard.cron_som_regen_sweep', raise_if_not_found=False)
    if cron:
        cron.sudo().write({'nextcall': datetime.now() + timedelta(minutes=2), 'active': True})
        _logger.info('[sale_delivery_wizard 11.11.1] barrido de regeneración programado (cron en 2 min)')
