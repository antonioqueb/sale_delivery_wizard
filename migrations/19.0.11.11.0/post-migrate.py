# -*- coding: utf-8 -*-
"""19.0.11.11.0 — Barrido de pickings de regeneración duplicados.

La auto-reparación de la cadena de entrega creaba un OUT "(regeneración
pendiente)" nuevo en CADA apertura del wizard (V/147 llegó a 6 clones, cada
uno reservando el mismo pallet). Con los dos candados nuevos (demanda viva
que sí cuenta los OUT de regeneración + reutilización del picking vivo) se
recorre cada orden con pickings de regeneración vivos: se conserva uno, se
cancelan los clones (liberan reservas) y el conservado se ajusta al déficit
real.
"""
import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    orders = env['sale.order']._som_sweep_regen_pickings()
    _logger.info('[sale_delivery_wizard 11.11.0] barrido de regeneración en %d orden(es): %s',
                 len(orders), ', '.join(orders.mapped('name')))
