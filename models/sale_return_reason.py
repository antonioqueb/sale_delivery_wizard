from odoo import api, fields, models

# Catálogo vigente de motivos de devolución (2 oct 2026). Sustituye al
# catálogo inicial de seis motivos, que queda archivado para no perder el
# motivo de las devoluciones ya registradas.
_RETURN_REASONS_V2 = [
    ('TRANSPORT_DAMAGE', 'Daño en transporte (Material roto o dañado durante transporte)'),
    ('LOADING_DAMAGE', 'Daño en carga (Material dañado durante carga en bodega)'),
    ('UNLOADING_DAMAGE', 'Daño en descarga (Material dañado durante descarga)'),
    ('ORDER_ERROR', 'Error de pedido (Material equivocado)'),
    ('QUALITY_V2', 'Calidad (Variación de color - Veta diferente - Mancha - Fisura - Irregularidad)'),
    ('CUSTOMER_DISLIKES', 'Material no agrada al cliente'),
    ('SURPLUS', 'Material sobrante'),
    ('MATERIAL_CHANGE', 'Cambio de material'),
    ('DIFFERENT_LOT', 'Lote diferente'),
    ('WRONG_FINISH', 'Acabado incorrecto'),
    ('PROJECT_CANCELLED', 'Cancelación de proyecto'),
    ('DELIVERY_NOT_CONFIRMED', 'Entrega no confirmada'),
    ('DELIVERY_DATA_ERROR', 'Error en datos de entrega/contacto'),
    ('ROAD_BLOCKED', 'Vialidad/acceso obstruido'),
    ('CUSTOMER_ABSENT', 'Cliente ausente / no recibe'),
    ('NOT_NEEDED', 'El cliente ya no necesita el material'),
    ('ACCESS_RESTRICTIONS', 'Restricciones de acceso (Horarios, permisos, restricciones de vehículo, etc.)'),
]
_RETURN_REASONS_V2_FLAG = 'sale_delivery_wizard.return_reasons_v2'


class SaleReturnReason(models.Model):
    _name = 'sale.return.reason'
    _description = 'Motivo de Devolución'
    _order = 'sequence, id'

    name = fields.Char(string='Motivo', required=True, translate=True)
    code = fields.Char(string='Código', required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    description = fields.Text(string='Descripción')

    @api.model
    def _som_apply_return_reasons_v2(self):
        """Reemplaza el catálogo UNA sola vez (los datos son noupdate): los
        motivos anteriores se archivan y nacen los vigentes. Después de
        aplicarse, el catálogo se administra desde el menú sin que una
        actualización del módulo lo vuelva a pisar."""
        params = self.env['ir.config_parameter'].sudo()
        if params.get_param(_RETURN_REASONS_V2_FLAG):
            return
        Reason = self.sudo().with_context(active_test=False)
        codes = [code for code, _name in _RETURN_REASONS_V2]
        Reason.search([('code', 'not in', codes), ('active', '=', True)]).write(
            {'active': False})
        for index, (code, name) in enumerate(_RETURN_REASONS_V2, start=1):
            vals = {'name': name, 'sequence': index * 10, 'active': True}
            reason = Reason.search([('code', '=', code)], limit=1)
            if reason:
                reason.write(vals)
            else:
                Reason.create(dict(vals, code=code))
        params.set_param(_RETURN_REASONS_V2_FLAG, '1')
