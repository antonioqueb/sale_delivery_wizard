# -*- coding: utf-8 -*-
"""Planificación de entregas — el VENDEDOR programa, LOGÍSTICA ejecuta.

Problema: la operación era reactiva (pasa algo → acción). Logística
terminaba buscando pagos, pidiendo autorizaciones y adivinando direcciones.

Diseño:
- `sale.delivery.schedule` ("Entrega programada"): la unidad de planeación.
  Nace desde la orden de venta con TODO lo necesario (contacto, teléfono,
  dirección, ubicación en el mapa, especificación y fecha). Sin eso no se
  puede programar: la constraint lo impide.
- Logística la ve en su Planificación (primera vista de Entregas): calendario
  semanal por día con arrastre para reprogramar. Cada movimiento queda en
  `sale.delivery.schedule.move` (de/a, quién, por qué, origen) y avisa al
  vendedor por el Centro de Actividades.
- El vendedor la ve en su propio menú raíz "Mis Entregas" (solo las suyas)
  con el historial de movimientos.
- Cron nocturno: lo programado para un día que ya pasó y no se entregó se
  recorre al día siguiente (origen 'rollover') y avisa a los usuarios de
  entregas con UNA actividad resumen.
- Liga con la operación: el pick ticket de la orden se engancha solo a la
  programación abierta más próxima (en proceso); la remisión hereda; la
  firma cierra la programación (entregada).
"""
import logging
from datetime import date as ddate, datetime, timedelta
from zoneinfo import ZoneInfo

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

MONTERREY = ZoneInfo('America/Monterrey')
MESES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']
DIAS = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']

TIME_WINDOWS = [
    ('any', 'Todo el día'),
    ('am', 'Mañana (9–13 h)'),
    ('pm', 'Tarde (13–18 h)'),
    ('exact', 'Hora exacta'),
]
STATES = [
    ('scheduled', 'Programada'),
    ('confirmed', 'Confirmada por logística'),
    ('in_progress', 'En proceso'),
    ('done', 'Entregada'),
    ('cancelled', 'Cancelada'),
]
OPEN_STATES = ('scheduled', 'confirmed', 'in_progress')


def _fmt_date(d):
    return '%d %s %d' % (d.day, MESES[d.month - 1], d.year) if d else ''


class SaleDeliverySchedule(models.Model):
    _name = 'sale.delivery.schedule'
    _description = 'Entrega programada'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date asc, time_window asc, time_exact asc, id asc'
    _rec_name = 'name'

    name = fields.Char('Folio', readonly=True, copy=False, default='Nuevo')
    sale_order_id = fields.Many2one(
        'sale.order', string='Orden de venta', required=True, index=True, ondelete='cascade',
        domain="[('state', 'in', ('sale', 'done'))]")
    partner_id = fields.Many2one(related='sale_order_id.partner_id', string='Cliente', store=True)
    company_id = fields.Many2one(related='sale_order_id.company_id', store=True)
    user_id = fields.Many2one(
        'res.users', string='Vendedor', index=True, tracking=True,
        default=lambda self: self.env.user)

    date = fields.Date('Fecha de entrega', required=True, index=True, tracking=True)
    original_date = fields.Date('Fecha original', readonly=True, copy=False)
    time_window = fields.Selection(TIME_WINDOWS, 'Horario', default='any', required=True, tracking=True)
    time_exact = fields.Float('Hora', help='Solo con horario "Hora exacta". 14.5 = 14:30.')

    contact_name = fields.Char('Contacto en sitio', required=True)
    contact_phone = fields.Char('Teléfono del contacto', required=True)
    delivery_address = fields.Text('Dirección de entrega', required=True)
    latitude = fields.Float('Latitud', digits=(10, 7))
    longitude = fields.Float('Longitud', digits=(10, 7))
    has_location = fields.Boolean('Con ubicación en mapa', compute='_compute_has_location', store=True)
    instructions = fields.Text(
        'Especificación de la entrega', required=True,
        help='Qué se entrega, cómo se recibe, accesos, horarios del sitio, quién recibe, '
             'maniobra, equipo necesario. Es lo que logística va a leer.')

    state = fields.Selection(STATES, 'Estado', default='scheduled', required=True, tracking=True, index=True)
    vehicle_id = fields.Many2one('fleet.vehicle', 'Vehículo', tracking=True)
    vehicle_driver_id = fields.Many2one('res.partner', 'Chofer', tracking=True)
    pick_ticket_id = fields.Many2one('sale.delivery.document', 'Pick ticket', readonly=True, copy=False)
    remission_id = fields.Many2one('sale.delivery.document', 'Remisión', readonly=True, copy=False)
    delivered_at = fields.Datetime('Entregada el', readonly=True, copy=False)
    cancel_reason = fields.Text('Motivo de cancelación', readonly=True, copy=False)

    move_ids = fields.One2many('sale.delivery.schedule.move', 'schedule_id', 'Movimientos', readonly=True)
    reschedule_count = fields.Integer('Reprogramaciones', compute='_compute_reschedule_count', store=True)

    qty_summary = fields.Char('Pendiente por entregar', compute='_compute_order_info')
    auth_ok = fields.Boolean('Entrega autorizada o pagada', compute='_compute_order_info')
    auth_label = fields.Char(compute='_compute_order_info')
    readiness = fields.Char('Pendientes para logística', compute='_compute_order_info')
    color = fields.Integer(compute='_compute_color')

    # ------------------------------------------------------------------
    # Cómputos
    # ------------------------------------------------------------------
    @api.depends('latitude', 'longitude')
    def _compute_has_location(self):
        for rec in self:
            rec.has_location = bool(rec.latitude and rec.longitude)

    @api.depends('move_ids')
    def _compute_reschedule_count(self):
        for rec in self:
            rec.reschedule_count = len(rec.move_ids.filtered(lambda m: m.kind == 'reschedule'))

    @api.depends('sale_order_id', 'sale_order_id.order_line.qty_delivered', 'state')
    def _compute_order_info(self):
        for rec in self:
            order = rec.sale_order_id
            by_uom = {}
            for line in order.order_line:
                if line.display_type or not line.product_id or line.product_id.type == 'service':
                    continue
                pending = (line.product_uom_qty or 0.0) - (line.qty_delivered or 0.0)
                if pending <= 0:
                    continue
                uom = line.product_uom_id.name if 'product_uom_id' in line._fields else line.product_uom.name
                by_uom[uom] = by_uom.get(uom, 0.0) + pending
            rec.qty_summary = ' · '.join('%g %s' % (round(q, 1), u) for u, q in by_uom.items()) or 'Nada pendiente'
            auth = getattr(order, 'delivery_auth_state', None)
            if auth is None:
                rec.auth_ok, rec.auth_label = True, ''
            else:
                rec.auth_ok = auth in ('authorized', 'paid')
                rec.auth_label = {
                    'paid': 'Pagada', 'authorized': 'Autorizada', 'requested': 'Autorización solicitada',
                }.get(auth, 'Sin autorización de entrega')
            issues = []
            if not rec.auth_ok:
                issues.append(rec.auth_label)
            if not rec.has_location:
                issues.append('Sin ubicación en mapa')
            if not rec.vehicle_id and rec.state in ('scheduled', 'confirmed'):
                issues.append('Sin camión')
            rec.readiness = ' · '.join(issues)

    @api.depends('state')
    def _compute_color(self):
        palette = {'scheduled': 4, 'confirmed': 10, 'in_progress': 2, 'done': 10, 'cancelled': 1}
        for rec in self:
            rec.color = palette.get(rec.state, 0)

    # ------------------------------------------------------------------
    # Reglas: sin información completa no hay programación
    # ------------------------------------------------------------------
    @api.constrains('contact_phone', 'delivery_address', 'instructions', 'latitude', 'longitude', 'state', 'date')
    def _check_complete(self):
        for rec in self.filtered(lambda r: r.state in ('scheduled', 'confirmed')):
            missing = []
            if not (rec.contact_phone or '').strip():
                missing.append('teléfono del contacto')
            if len((rec.delivery_address or '').strip()) < 10:
                missing.append('dirección de entrega completa')
            if len((rec.instructions or '').strip()) < 10:
                missing.append('especificación de la entrega')
            if not (rec.latitude and rec.longitude):
                missing.append('ubicación en el mapa')
            if missing:
                raise ValidationError(_(
                    'No se puede programar la entrega de %s sin: %s.\n'
                    'Logística solo ejecuta: la información completa la da el vendedor.'
                ) % (rec.sale_order_id.name, ', '.join(missing)))
            if rec.sale_order_id.state not in ('sale', 'done'):
                raise ValidationError(_('Solo se programan entregas de órdenes confirmadas.'))

    @api.constrains('time_window', 'time_exact')
    def _check_time(self):
        for rec in self:
            if rec.time_window == 'exact' and not (0 <= (rec.time_exact or 0) < 24):
                raise ValidationError(_('Captura una hora válida (0–23:59).'))

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'Nuevo') == 'Nuevo':
                vals['name'] = self.env['ir.sequence'].next_by_code('sale.delivery.schedule') or 'Nuevo'
            if vals.get('sale_order_id') and not vals.get('user_id'):
                order = self.env['sale.order'].browse(vals['sale_order_id'])
                vals['user_id'] = order.user_id.id or self.env.uid
            if vals.get('date') and not vals.get('original_date'):
                vals['original_date'] = vals['date']
        records = super().create(vals_list)
        for rec in records:
            rec.sale_order_id.message_post(body=_(
                '📅 Entrega <b>programada</b> para el <b>%s</b> (%s) por %s. Folio %s.'
            ) % (_fmt_date(rec.date), dict(TIME_WINDOWS)[rec.time_window], self.env.user.name, rec.name),
                message_type='notification', subtype_xmlid='mail.mt_note')
        return records

    def write(self, vals):
        # La fecha solo cambia por action_reschedule (deja historial). Un
        # write directo con otra fecha se registra igual como reprogramación.
        if 'date' in vals and not self.env.context.get('som_schedule_move'):
            new_date = fields.Date.to_date(vals['date'])
            for rec in self:
                if rec.date != new_date:
                    rec._log_move('reschedule', rec.date, new_date, _('Cambio directo de fecha'), 'manual')
        return super().write(vals)

    def _log_move(self, kind, date_from, date_to, reason, source):
        self.ensure_one()
        return self.env['sale.delivery.schedule.move'].sudo().create({
            'schedule_id': self.id,
            'kind': kind,
            'date_from': date_from,
            'date_to': date_to,
            'reason': reason or '',
            'source': source,
            'user_id': self.env.uid,
        })

    def _is_delivery_staff(self):
        u = self.env.user
        return u.has_group('sale_delivery_wizard.group_delivery_user')

    def action_reschedule(self, new_date, reason='', source='logistics'):
        """Mueve la entrega a otra fecha dejando historial y avisando."""
        new_date = fields.Date.to_date(new_date)
        if not new_date:
            raise UserError(_('Indica la nueva fecha.'))
        for rec in self:
            if rec.state not in OPEN_STATES:
                raise UserError(_('%s ya está %s; no se reprograma.') % (rec.name, dict(STATES)[rec.state].lower()))
            if rec.date == new_date:
                continue
            old = rec.date
            rec._log_move('reschedule', old, new_date, reason, source)
            rec.with_context(som_schedule_move=True).write({'date': new_date})
            who = self.env.user.name
            body = _('📅 Entrega <b>reprogramada</b> del %s al <b>%s</b> por %s.%s') % (
                _fmt_date(old), _fmt_date(new_date), who,
                (' Motivo: %s' % reason) if reason else '')
            rec.message_post(body=body, message_type='notification', subtype_xmlid='mail.mt_note')
            rec.sale_order_id.message_post(body=body, message_type='notification', subtype_xmlid='mail.mt_note')
            # Aviso al vendedor cuando NO fue él quien movió (Centro de Actividades).
            if rec.user_id and rec.user_id != self.env.user and source != 'seller':
                rec.activity_schedule(
                    'mail.mail_activity_data_todo', user_id=rec.user_id.id,
                    summary=_('Entrega reprogramada: %s · %s') % (rec.sale_order_id.name, rec.partner_id.name or ''),
                    note=_('<p>%s movió la entrega <b>%s</b> del %s al <b>%s</b>.</p><p>%s</p>') % (
                        who, rec.name, _fmt_date(old), _fmt_date(new_date),
                        ('Motivo: %s' % reason) if reason else 'Sin motivo capturado.'),
                    date_deadline=new_date)
        return True

    def action_confirm(self):
        for rec in self:
            if rec.state != 'scheduled':
                raise UserError(_('Solo se confirman entregas programadas.'))
            rec.state = 'confirmed'
            rec.message_post(body=_('✅ Confirmada por logística (%s).') % self.env.user.name,
                             message_type='notification', subtype_xmlid='mail.mt_note')
        return True

    def action_cancel(self, reason=None):
        reason = (reason or self.env.context.get('cancel_reason') or '').strip()
        for rec in self:
            if rec.state == 'done':
                raise UserError(_('Una entrega ya realizada no se cancela.'))
            rec._log_move('cancel', rec.date, False, reason, 'seller' if not rec._is_delivery_staff() else 'logistics')
            rec.write({'state': 'cancelled', 'cancel_reason': reason})
            body = _('⛔ Programación <b>cancelada</b> por %s.%s') % (
                self.env.user.name, (' Motivo: %s' % reason) if reason else '')
            rec.message_post(body=body, message_type='notification', subtype_xmlid='mail.mt_note')
            rec.sale_order_id.message_post(body=body, message_type='notification', subtype_xmlid='mail.mt_note')
        return True

    def action_reopen(self):
        for rec in self:
            if rec.state != 'cancelled':
                continue
            rec.write({'state': 'scheduled', 'cancel_reason': False})
        return True

    def action_mark_done(self, delivered_at=None):
        for rec in self:
            if rec.state == 'done':
                continue
            rec.write({'state': 'done', 'delivered_at': delivered_at or fields.Datetime.now()})
            rec.message_post(body=_('📦 Entrega <b>realizada</b>.'), message_type='notification',
                             subtype_xmlid='mail.mt_note')
        return True

    def action_open_order(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'res_model': 'sale.order', 'res_id': self.sale_order_id.id,
            'view_mode': 'form', 'target': 'current',
        }

    def action_generate_delivery(self):
        """Logística ejecuta: abre el asistente de entrega de la orden con la
        dirección y el camión de la programación ya puestos."""
        self.ensure_one()
        if self.state not in OPEN_STATES:
            raise UserError(_('La programación %s ya está %s.') % (self.name, dict(STATES)[self.state].lower()))
        action = self.sale_order_id.with_context(
            default_delivery_address=self._address_for_wizard(),
            default_special_instructions=self.instructions,
            default_vehicle_id=self.vehicle_id.id,
            default_vehicle_driver_id=self.vehicle_driver_id.id,
            som_schedule_id=self.id,
        ).action_open_delivery_wizard()
        if isinstance(action, dict):
            ctx = dict(action.get('context') or {})
            ctx.update({
                'default_delivery_address': self._address_for_wizard(),
                'default_special_instructions': self.instructions,
                'som_schedule_id': self.id,
            })
            if self.vehicle_id:
                ctx['default_vehicle_id'] = self.vehicle_id.id
            if self.vehicle_driver_id:
                ctx['default_vehicle_driver_id'] = self.vehicle_driver_id.id
            action['context'] = ctx
        return action

    def _address_for_wizard(self):
        self.ensure_one()
        parts = [self.contact_name or '', self.delivery_address or '']
        if self.contact_phone:
            parts.append('Tel. %s' % self.contact_phone)
        if self.has_location:
            parts.append('Mapa: https://maps.google.com/?q=%s,%s' % (self.latitude, self.longitude))
        return '\n'.join(p for p in parts if p)

    def action_open_map(self):
        self.ensure_one()
        if not self.has_location:
            raise UserError(_('Esta programación no tiene ubicación en el mapa.'))
        return {'type': 'ir.actions.act_url', 'target': 'new',
                'url': 'https://maps.google.com/?q=%s,%s' % (self.latitude, self.longitude)}

    # ------------------------------------------------------------------
    # Liga con la operación (pick ticket / remisión / firma)
    # ------------------------------------------------------------------
    @api.model
    def _find_open_for_order(self, order):
        return self.search([
            ('sale_order_id', '=', order.id), ('state', 'in', OPEN_STATES),
        ], order='date asc, id asc', limit=1)

    def _link_pick_ticket(self, doc):
        for rec in self:
            vals = {'pick_ticket_id': doc.id}
            if rec.state in ('scheduled', 'confirmed'):
                vals['state'] = 'in_progress'
            if doc.vehicle_id and not rec.vehicle_id:
                vals['vehicle_id'] = doc.vehicle_id.id
            if doc.vehicle_driver_id and not rec.vehicle_driver_id:
                vals['vehicle_driver_id'] = doc.vehicle_driver_id.id
            rec.write(vals)

    def _link_remission(self, doc):
        for rec in self:
            vals = {'remission_id': doc.id}
            if rec.state in ('scheduled', 'confirmed'):
                vals['state'] = 'in_progress'
            if doc.vehicle_id:
                vals['vehicle_id'] = doc.vehicle_id.id
            if doc.vehicle_driver_id:
                vals['vehicle_driver_id'] = doc.vehicle_driver_id.id
            rec.write(vals)

    # ------------------------------------------------------------------
    # Cron: lo no entregado se recorre al día siguiente y se avisa
    # ------------------------------------------------------------------
    @api.model
    def _cron_rollover(self):
        today = datetime.now(MONTERREY).date()
        stale = self.search([('date', '<', today), ('state', 'in', OPEN_STATES)])
        if not stale:
            return 0
        moved = self.env['sale.delivery.schedule']
        for rec in stale:
            old = rec.date
            try:
                rec.with_context(som_schedule_move=True)._rollover_one(old, today)
                moved |= rec
            except Exception:  # noqa: BLE001 — una programación rota no detiene el barrido
                _logger.exception('[PLANIFICACIÓN] no se pudo recorrer %s', rec.name)
        if moved:
            self._notify_rollover(moved, today)
        _logger.info('[PLANIFICACIÓN] rollover: %s entrega(s) recorrida(s) a %s', len(moved), today)
        return len(moved)

    def _rollover_one(self, old, today):
        self.ensure_one()
        self._log_move('reschedule', old, today, _('No se entregó el %s') % _fmt_date(old), 'rollover')
        self.write({'date': today})
        body = _('⏭️ Entrega <b>no realizada</b> el %s: se recorre automáticamente al <b>%s</b>.') % (
            _fmt_date(old), _fmt_date(today))
        self.message_post(body=body, message_type='notification', subtype_xmlid='mail.mt_note')
        self.sale_order_id.message_post(body=body, message_type='notification', subtype_xmlid='mail.mt_note')
        if self.user_id:
            self.activity_schedule(
                'mail.mail_activity_data_todo', user_id=self.user_id.id,
                summary=_('Entrega reprogramada: %s · %s') % (self.sale_order_id.name, self.partner_id.name or ''),
                note=_('<p>La entrega <b>%s</b> no se realizó el %s y se recorrió al <b>%s</b>. '
                       'Revisa con el cliente y ajusta la fecha si hace falta.</p>') % (
                    self.name, _fmt_date(old), _fmt_date(today)),
                date_deadline=today)

    @api.model
    def _delivery_users(self):
        group = self.env.ref('sale_delivery_wizard.group_delivery_user', raise_if_not_found=False)
        if not group:
            return self.env['res.users']
        users = self.env['res.users']
        for fname in ('all_user_ids', 'user_ids'):
            if fname in group._fields:
                users |= group[fname]
        return users.filtered(lambda u: u.active and not u.share)

    def _notify_rollover(self, moved, today):
        """UNA actividad resumen por usuario de entregas (no una por entrega)."""
        todo = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)
        lines = ''.join('<li><b>%s</b> · %s · %s (programada el %s)</li>' % (
            r.sale_order_id.name, r.partner_id.name or '', r.name,
            _fmt_date(r.move_ids.filtered(lambda m: m.source == 'rollover')[:1].date_from or r.original_date))
            for r in moved)
        note = _('<p>%d entrega(s) programadas no se realizaron y se recorrieron a hoy (%s):</p><ul>%s</ul>'
                 '<p>Revísalas en Entregas › Planificación.</p>') % (len(moved), _fmt_date(today), lines)
        summary = _('Entregas no realizadas recorridas a hoy: %d') % len(moved)
        Activity = self.env['mail.activity'].sudo()
        for user in self._delivery_users():
            Activity.with_context(mail_activity_quick_update=True).create({
                'activity_type_id': todo.id if todo else False,
                'user_id': user.id,
                'summary': summary,
                'note': note,
                'date_deadline': today,
            })

    # ------------------------------------------------------------------
    # Planificador (client action)
    # ------------------------------------------------------------------
    @api.model
    def _week_start(self, iso_date=None):
        base = fields.Date.to_date(iso_date) if iso_date else datetime.now(MONTERREY).date()
        return base - timedelta(days=base.weekday())

    @api.model
    def planner_data(self, week_start=None, only_mine=False):
        start = self._week_start(week_start)
        return self.planner_range(start.isoformat(), (start + timedelta(days=6)).isoformat(), only_mine)

    @api.model
    def planner_range(self, date_from, date_to, only_mine=False):
        """Días con sus entregas para cualquier rango (día, semana o mes):
        el cliente decide el rango; aquí solo se arma un día por fecha."""
        start = fields.Date.to_date(date_from)
        end = fields.Date.to_date(date_to)
        if not start or not end or end < start:
            raise UserError(_('Rango de fechas inválido.'))
        if (end - start).days > 62:
            raise UserError(_('El rango máximo es de dos meses.'))
        today = datetime.now(MONTERREY).date()
        domain = [('date', '>=', start), ('date', '<=', end)]
        if only_mine:
            domain.append(('user_id', '=', self.env.uid))
        records = self.search(domain)
        by_day = {start + timedelta(days=i): [] for i in range((end - start).days + 1)}
        for rec in records:
            by_day[rec.date].append(self._planner_card(rec))
        overdue = self.search_count([('date', '<', today), ('state', 'in', OPEN_STATES)] +
                                    ([('user_id', '=', self.env.uid)] if only_mine else []))
        days = []
        for d, cards in by_day.items():
            open_cards = [c for c in cards if c['state'] in OPEN_STATES]
            days.append({
                'iso': d.isoformat(),
                'label': '%s %d %s' % (DIAS[d.weekday()], d.day, MESES[d.month - 1]),
                'day': d.day,
                'month': d.month,
                'weekday': d.weekday(),
                'is_today': d == today,
                'is_past': d < today,
                'cards': cards,
                'open_count': len(open_cards),
                'done_count': len([c for c in cards if c['state'] == 'done']),
                'issues': len([c for c in open_cards if c['readiness']]),
            })
        return {
            'week_start': start.isoformat(),
            'date_from': start.isoformat(),
            'date_to': end.isoformat(),
            'week_label': '%d %s – %d %s %d' % (start.day, MESES[start.month - 1], end.day, MESES[end.month - 1], end.year),
            'today': today.isoformat(),
            'days': days,
            'overdue': overdue,
            'is_staff': self.env.user.has_group('sale_delivery_wizard.group_delivery_user'),
            'can_generate': self.env.user.has_group('sale_delivery_wizard.group_delivery_user'),
        }

    def _planner_card(self, rec):
        tw = dict(TIME_WINDOWS)[rec.time_window]
        if rec.time_window == 'exact':
            h = int(rec.time_exact or 0)
            m = int(round(((rec.time_exact or 0) - h) * 60))
            tw = '%02d:%02d h' % (h, m)
        last_move = rec.move_ids.filtered(lambda mv: mv.kind == 'reschedule')[:1]
        return {
            'id': rec.id,
            'name': rec.name,
            'order': rec.sale_order_id.name,
            'order_id': rec.sale_order_id.id,
            'partner': rec.partner_id.name or '',
            'seller': rec.user_id.name or '',
            'contact': rec.contact_name or '',
            'phone': rec.contact_phone or '',
            'address': (rec.delivery_address or '').replace('\n', ', ')[:120],
            'has_location': rec.has_location,
            'lat': rec.latitude,
            'lng': rec.longitude,
            'time_label': tw,
            'time_window': rec.time_window,
            'time_exact': rec.time_exact,
            'state': rec.state,
            'state_label': dict(STATES)[rec.state],
            'auth_ok': rec.auth_ok,
            'auth_label': rec.auth_label,
            'readiness': rec.readiness,
            'vehicle': rec.vehicle_id.display_name if rec.vehicle_id else '',
            'driver': rec.vehicle_driver_id.display_name if rec.vehicle_driver_id else '',
            'qty': rec.qty_summary,
            'instructions': (rec.instructions or '')[:220],
            'reschedules': rec.reschedule_count,
            'last_move': ('%s → %s (%s)' % (_fmt_date(last_move.date_from), _fmt_date(last_move.date_to),
                                             last_move.user_id.name or '')) if last_move else '',
            'pt': rec.pick_ticket_id.name or '',
            'rem': (rec.remission_id.remission_number or rec.remission_id.name) if rec.remission_id else '',
            'delivered_at': fields.Datetime.context_timestamp(self, rec.delivered_at).strftime('%H:%M') if rec.delivered_at else '',
        }

    @api.model
    def planner_move(self, schedule_id, new_date, reason=''):
        rec = self.browse(int(schedule_id)).exists()
        if not rec:
            return {'error': _('La programación ya no existe.')}
        source = 'logistics' if rec._is_delivery_staff() else 'seller'
        try:
            rec.action_reschedule(new_date, reason=reason, source=source)
        except (UserError, ValidationError) as exc:
            return {'error': str(exc)}
        return {'ok': True}

    @api.model
    def planner_confirm(self, schedule_id):
        rec = self.browse(int(schedule_id)).exists()
        if not rec:
            return {'error': _('La programación ya no existe.')}
        try:
            rec.action_confirm()
        except UserError as exc:
            return {'error': str(exc)}
        return {'ok': True}


class SaleDeliveryScheduleMove(models.Model):
    _name = 'sale.delivery.schedule.move'
    _description = 'Movimiento de entrega programada'
    _order = 'create_date desc, id desc'

    schedule_id = fields.Many2one('sale.delivery.schedule', required=True, index=True, ondelete='cascade')
    kind = fields.Selection([('reschedule', 'Reprogramación'), ('cancel', 'Cancelación')], required=True, default='reschedule')
    date_from = fields.Date('De')
    date_to = fields.Date('A')
    reason = fields.Text('Motivo')
    source = fields.Selection([
        ('seller', 'Vendedor'), ('logistics', 'Logística'), ('rollover', 'Automático (no entregada)'), ('manual', 'Cambio directo'),
    ], required=True, default='manual')
    user_id = fields.Many2one('res.users', 'Quién', default=lambda self: self.env.user, readonly=True)
    company_id = fields.Many2one(related='schedule_id.company_id', store=True)


class SaleDeliveryScheduleMoveWizard(models.TransientModel):
    _name = 'sale.delivery.schedule.move.wizard'
    _description = 'Reprogramar entrega'

    schedule_id = fields.Many2one('sale.delivery.schedule', required=True, readonly=True)
    current_date = fields.Date(related='schedule_id.date')
    new_date = fields.Date('Nueva fecha', required=True)
    reason = fields.Text('Motivo', required=True)

    def action_confirm(self):
        self.ensure_one()
        source = 'logistics' if self.schedule_id._is_delivery_staff() else 'seller'
        self.schedule_id.action_reschedule(self.new_date, reason=self.reason, source=source)
        return {'type': 'ir.actions.act_window_close'}


class SaleDeliveryScheduleCancelWizard(models.TransientModel):
    _name = 'sale.delivery.schedule.cancel.wizard'
    _description = 'Cancelar entrega programada'

    schedule_id = fields.Many2one('sale.delivery.schedule', required=True, readonly=True)
    reason = fields.Text('Motivo', required=True)

    def action_confirm(self):
        self.ensure_one()
        self.schedule_id.action_cancel(reason=self.reason)
        return {'type': 'ir.actions.act_window_close'}


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    delivery_schedule_ids = fields.One2many('sale.delivery.schedule', 'sale_order_id', 'Entregas programadas')
    delivery_schedule_count = fields.Integer(compute='_compute_delivery_schedule_count')
    next_delivery_date = fields.Date('Próxima entrega', compute='_compute_delivery_schedule_count')

    @api.depends('delivery_schedule_ids.state', 'delivery_schedule_ids.date')
    def _compute_delivery_schedule_count(self):
        for order in self:
            open_ones = order.delivery_schedule_ids.filtered(lambda s: s.state in OPEN_STATES).sorted('date')
            order.delivery_schedule_count = len(order.delivery_schedule_ids)
            order.next_delivery_date = open_ones[:1].date if open_ones else False

    def _som_schedule_defaults(self):
        self.ensure_one()
        partner = self.partner_shipping_id or self.partner_id
        phone = partner.phone or self.partner_id.phone or ''
        try:
            address = self._som_get_delivery_address_text()
        except Exception:  # noqa: BLE001 — RedirectWarning por contacto incompleto: se captura a mano
            address = partner.contact_address or ''
        return {
            'default_sale_order_id': self.id,
            'default_user_id': self.user_id.id or self.env.uid,
            'default_contact_name': partner.name or self.partner_id.name,
            'default_contact_phone': phone,
            'default_delivery_address': address,
            'default_latitude': getattr(partner, 'partner_latitude', 0.0) or 0.0,
            'default_longitude': getattr(partner, 'partner_longitude', 0.0) or 0.0,
            'default_date': (datetime.now(MONTERREY).date() + timedelta(days=1)).isoformat(),
        }

    def action_schedule_delivery(self):
        """Botón «Programar entrega» del vendedor: abre la programación con
        todo lo que ya se sabe de la orden para completar lo que falta."""
        self.ensure_one()
        if self.state not in ('sale', 'done'):
            raise UserError(_('Solo se programan entregas de órdenes confirmadas.'))
        open_one = self.env['sale.delivery.schedule']._find_open_for_order(self)
        if open_one:
            return {
                'type': 'ir.actions.act_window', 'name': _('Entrega programada'),
                'res_model': 'sale.delivery.schedule', 'res_id': open_one.id,
                'view_mode': 'form', 'target': 'current',
            }
        return {
            'type': 'ir.actions.act_window', 'name': _('Programar entrega'),
            'res_model': 'sale.delivery.schedule', 'view_mode': 'form', 'target': 'current',
            'context': self._som_schedule_defaults(),
        }

    def action_view_delivery_schedules(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('Entregas programadas'),
            'res_model': 'sale.delivery.schedule', 'view_mode': 'list,calendar,form',
            'domain': [('sale_order_id', '=', self.id)],
            'context': {'default_sale_order_id': self.id},
        }


class SaleDeliveryDocument(models.Model):
    _inherit = 'sale.delivery.document'

    schedule_id = fields.Many2one('sale.delivery.schedule', 'Entrega programada', index=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        docs = super().create(vals_list)
        Schedule = self.env['sale.delivery.schedule'].sudo()
        for doc in docs:
            try:
                if doc.document_type == 'pick_ticket':
                    sched = Schedule.browse(self.env.context.get('som_schedule_id')).exists() \
                        if self.env.context.get('som_schedule_id') else Schedule
                    if not sched or sched.sale_order_id != doc.sale_order_id:
                        sched = Schedule._find_open_for_order(doc.sale_order_id)
                    if sched:
                        doc.schedule_id = sched.id
                        sched._link_pick_ticket(doc)
                elif doc.document_type == 'remission':
                    sched = doc.pick_ticket_id.schedule_id or Schedule._find_open_for_order(doc.sale_order_id)
                    if sched:
                        doc.schedule_id = sched.id
                        sched._link_remission(doc)
            except Exception:  # noqa: BLE001 — la liga es informativa; jamás bloquea la operación
                _logger.exception('[PLANIFICACIÓN] no se pudo ligar %s a su programación', doc.name)
        return docs

    def write(self, vals):
        res = super().write(vals)
        if vals.get('signed_at'):
            for doc in self.filtered(lambda d: d.document_type == 'remission' and d.schedule_id):
                try:
                    doc.schedule_id.sudo().action_mark_done(delivered_at=doc.signed_at)
                except Exception:  # noqa: BLE001
                    _logger.exception('[PLANIFICACIÓN] no se pudo cerrar la programación de %s', doc.name)
        if vals.get('state') == 'cancelled':
            for doc in self.filtered(lambda d: d.schedule_id and d.schedule_id.state == 'in_progress'):
                sched = doc.schedule_id.sudo()
                if doc.document_type == 'pick_ticket' and sched.pick_ticket_id == doc:
                    sched.write({'pick_ticket_id': False, 'state': 'scheduled'})
                elif doc.document_type == 'remission' and sched.remission_id == doc:
                    sched.write({'remission_id': False, 'state': 'scheduled' if not sched.pick_ticket_id else 'in_progress'})
        return res
