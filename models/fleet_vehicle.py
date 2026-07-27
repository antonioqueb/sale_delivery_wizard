# -*- coding: utf-8 -*-
from odoo import fields, models


class FleetVehicle(models.Model):
    _inherit = 'fleet.vehicle'

    x_capacity_sqm = fields.Float(
        string='Capacidad (m²)',
        digits=(12, 2),
        help='Capacidad de carga del vehículo en metros cuadrados de material. '
             'Se usa en las remisiones para registrar la ocupación de cada '
             'viaje y sacar indicadores de aprovechamiento.',
    )
