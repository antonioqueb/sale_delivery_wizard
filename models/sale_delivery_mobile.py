# -*- coding: utf-8 -*-
"""Extensión móvil del Delivery Wizard (app STO Scanner).

Flujo de salidas desde el teléfono:
  1. El chofer/almacenista escanea el barcode del Pick Ticket impreso.
  2. Escanea una a una las placas físicas contra las líneas del PT.
  3. Con evidencia fotográfica obligatoria, genera la remisión desde el
     teléfono (reutilizando la lógica completa del wizard).
  4. Al entregar: firma en pantalla + foto de descarga + GPS; se inyecta
     al documento y se envía la remisión firmada por correo al cliente.

También define el registro de puntos GPS (sale.delivery.route.point) y el
mapa en vivo de entregas (sale.delivery.live.map).
"""
import logging
import math
from datetime import datetime, time as dtime

import pytz

from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SaleDeliveryDocument(models.Model):
    _inherit = 'sale.delivery.document'

    # Trazabilidad PT ↔ remisión para el flujo móvil (el wizard web no la
    # guarda; aquí sí, porque el chofer parte del PT escaneado).
    pick_ticket_id = fields.Many2one(
        'sale.delivery.document',
        string='Pick Ticket Origen',
        readonly=True,
        copy=False,
        index=True,
        domain=[('document_type', '=', 'pick_ticket')],
    )
    signed_at = fields.Datetime(string='Firmado el', readonly=True, copy=False)
    delivered_by = fields.Char(string='Entregó (nombre)', copy=False)
    delivered_signature_image = fields.Binary(
        string='Firma de quien entrega', attachment=True, copy=False)
    signed_latitude = fields.Float(string='Latitud de Firma', digits=(10, 7), readonly=True, copy=False)
    signed_longitude = fields.Float(string='Longitud de Firma', digits=(10, 7), readonly=True, copy=False)

    # ──────────────────────────────────────────────
    # RPC móvil: cargar el pick ticket escaneado
    # ──────────────────────────────────────────────

    @api.model
    def app_get_pick_ticket(self, name):
        """Pick ticket por folio (barcode escaneado). Devuelve cabecera y
        líneas con lote/producto/cantidad para verificación física."""
        pt = self.search([
            ('name', '=', name),
            ('document_type', '=', 'pick_ticket'),
        ], limit=1)
        if not pt:
            return {'found': False, 'error': _('Pick ticket "%s" no existe') % name}
        if pt.state == 'cancelled':
            return {'found': False, 'error': _('El pick ticket %s está cancelado') % pt.name}
        if pt.state == 'confirmed':
            return {'found': False, 'error': _(
                'El pick ticket %s ya fue surtido (remisión generada)') % pt.name}

        lines = []
        for line in pt.line_ids:
            lines.append({
                'line_id': line.id,
                'lot_id': line.lot_id.id,
                'lot_name': line.lot_id.name or '',
                'product_id': line.product_id.id,
                'product_name': line.product_id.display_name,
                'qty': line.qty_selected,
                'location': line._format_short_location() if hasattr(line, '_format_short_location') else (line.source_location_id.complete_name or ''),
            })

        order = pt.sale_order_id
        return {
            'found': True,
            'id': pt.id,
            'name': pt.name,
            'state': pt.state,
            'sale_order': order.name,
            'partner': order.partner_id.display_name,
            'partner_email': order.partner_id.email or '',
            'delivery_address': pt.delivery_address or '',
            'special_instructions': pt.special_instructions or '',
            'vehicle': pt.vehicle_id.display_name if pt.vehicle_id else '',
            'driver': pt.vehicle_driver_id.display_name if pt.vehicle_driver_id else '',
            'delivery_auth_state': getattr(order, 'delivery_auth_state', '') or '',
            'lines': lines,
        }

    # ──────────────────────────────────────────────
    # RPC móvil: generar la remisión desde el escaneo
    # ──────────────────────────────────────────────

    @api.model
    def app_generate_remission_from_pt(self, pick_ticket_id, scanned_lot_ids):
        """Genera la(s) remisión(es) desde el teléfono reutilizando la
        lógica completa del wizard (candados de demanda, autorización de
        pago, validación de picking, agrupación por picking).

        Exige que TODAS las placas del PT estén escaneadas — el control
        físico es el punto de esta herramienta.
        """
        pt = self.browse(pick_ticket_id)
        if not pt.exists() or pt.document_type != 'pick_ticket':
            raise UserError(_('Pick ticket inválido'))
        if pt.state != 'prepared':
            raise UserError(_(
                'El pick ticket %s no está listo para surtir (estado: %s)'
            ) % (pt.name, pt.state))

        expected = set(pt.line_ids.mapped('lot_id').ids)
        scanned = set(scanned_lot_ids or [])
        missing = expected - scanned
        if missing:
            names = ', '.join(
                self.env['stock.lot'].browse(list(missing)).mapped('name'))
            raise UserError(_(
                'Faltan placas por escanear: %s.\n'
                'Escanea físicamente todo el pick ticket antes de generar '
                'la remisión.') % names)
        extra = scanned - expected
        if extra:
            names = ', '.join(
                self.env['stock.lot'].browse(list(extra)).mapped('name'))
            raise UserError(_(
                'Estas placas NO pertenecen al pick ticket: %s') % names)

        before = self.search([
            ('sale_order_id', '=', pt.sale_order_id.id),
            ('document_type', '=', 'remission'),
        ])

        wizard = self.env['sale.delivery.wizard'].create({
            'sale_order_id': pt.sale_order_id.id,
            'pick_ticket_id': pt.id,
            'delivery_address': pt.delivery_address or '',
            'special_instructions': pt.special_instructions or '',
            'vehicle_id': pt.vehicle_id.id or False,
            'vehicle_driver_id': pt.vehicle_driver_id.id or False,
        })
        wizard._generate_remission_from_pick_ticket()

        after = self.search([
            ('sale_order_id', '=', pt.sale_order_id.id),
            ('document_type', '=', 'remission'),
        ])
        new_docs = after - before
        new_docs.write({'pick_ticket_id': pt.id})

        for doc in new_docs:
            doc.message_post(body=_(
                'Remisión generada desde la app móvil (escaneo físico '
                'completo del pick ticket %s).') % pt.name)

        return [{
            'id': d.id,
            'name': d.remission_number or d.name,
            'state': d.state,
        } for d in new_docs]

    # ──────────────────────────────────────────────
    # RPC móvil: evidencia, firma y correo
    # ──────────────────────────────────────────────

    @api.model
    def app_add_delivery_media(self, doc_id, payload):
        """Adjunta evidencia/firma desde el teléfono.

        payload = {
          'photos': [{'name': str, 'data': b64}],      # evidencia
          'signed_by': str,                             # nombre del receptor
          'signature': b64 PNG,                         # firma táctil
          'latitude': float, 'longitude': float,        # GPS del momento
          'send_email': bool,                           # correo al firmar
        }
        """
        doc = self.browse(doc_id)
        if not doc.exists():
            raise UserError(_('Documento de entrega inexistente'))

        payload = payload or {}
        attach_ids = []
        for photo in payload.get('photos') or []:
            att = self.env['ir.attachment'].create({
                'name': photo.get('name') or 'evidencia.jpg',
                'datas': photo.get('data'),
                'res_model': 'sale.delivery.document',
                'res_id': doc.id,
                'mimetype': 'image/jpeg',
            })
            attach_ids.append(att.id)
        if attach_ids:
            doc.write({'attachment_ids': [(4, a) for a in attach_ids]})

        vals = {}
        signed = bool(payload.get('signature'))
        if signed:
            # Garantía: la entrega firmada requiere AMBAS firmas
            if not payload.get('delivery_signature'):
                raise UserError(_(
                    'Falta la firma de quien ENTREGA. Ambas firmas son '
                    'obligatorias para cerrar la entrega.'))
            vals.update({
                'signature_image': payload['signature'],
                'signed_by': payload.get('signed_by') or '',
                'delivered_signature_image': payload['delivery_signature'],
                'delivered_by': payload.get('delivered_by') or '',
                'signed_at': fields.Datetime.now(),
                'signed_latitude': payload.get('latitude') or 0.0,
                'signed_longitude': payload.get('longitude') or 0.0,
            })
        if vals:
            doc.write(vals)

        # Bitácora con GPS
        lat, lng = payload.get('latitude'), payload.get('longitude')
        gps_txt = ''
        if lat and lng:
            gps_txt = _(' — GPS: %(lat).6f, %(lng).6f '
                        '(https://maps.google.com/?q=%(lat).6f,%(lng).6f)') % {
                'lat': lat, 'lng': lng}
        if signed:
            doc.message_post(body=_(
                'Entrega FIRMADA desde la app por "%(who)s"%(gps)s. '
                'Fotos de evidencia: %(n)d.') % {
                'who': vals.get('signed_by') or '-',
                'gps': gps_txt, 'n': len(attach_ids)})
        elif attach_ids:
            doc.message_post(body=_(
                'Evidencia de salida cargada desde la app (%(n)d fotos)%(gps)s.'
            ) % {'n': len(attach_ids), 'gps': gps_txt})

        # Correo automático con la remisión firmada
        email_sent = False
        if signed and payload.get('send_email', True):
            email_sent = doc._app_send_signed_email()

        return {'ok': True, 'photos': len(attach_ids),
                'signed': signed, 'email_sent': email_sent}

    def _app_send_signed_email(self):
        """Envía la remisión firmada (PDF con firma) al contacto del pedido."""
        self.ensure_one()
        partner = self.sale_order_id.partner_id
        if not partner.email:
            self.message_post(body=_(
                'No se envió correo: el cliente %s no tiene email.') % partner.name)
            return False
        try:
            template = self.env.ref(
                'sale_delivery_wizard.mail_template_remission_signed',
                raise_if_not_found=False)
            if not template:
                return False
            # La evidencia fotográfica de la entrega viaja en el correo
            # junto con la remisión firmada en PDF
            email_values = {}
            if self.attachment_ids:
                email_values['attachment_ids'] = [(6, 0, self.attachment_ids.ids)]
            template.send_mail(self.id, force_send=True,
                               email_values=email_values or None)
            self.message_post(body=_(
                'Remisión firmada enviada por correo a %(mail)s '
                '(con %(n)d fotos de evidencia).') % {
                'mail': partner.email, 'n': len(self.attachment_ids)})
            return True
        except Exception as e:
            _logger.warning('Correo de remisión firmada falló (%s): %s',
                            self.name, e)
            self.message_post(body=_(
                'El correo de la remisión firmada no pudo enviarse: %s') % e)
            return False


class SaleDeliveryRoutePoint(models.Model):
    """Punto GPS de una entrega en curso — para rutas, tiempos y el mapa
    en vivo. Los registra la app móvil solo durante salidas activas."""
    _name = 'sale.delivery.route.point'
    _description = 'Punto GPS de Entrega'
    _order = 'timestamp desc'

    document_id = fields.Many2one(
        'sale.delivery.document', string='Documento de Entrega',
        required=True, ondelete='cascade', index=True)
    user_id = fields.Many2one(
        'res.users', string='Usuario (chofer)', required=True,
        default=lambda self: self.env.user, index=True)
    event_type = fields.Selection([
        ('inicio', 'Inicio de ruta'),
        ('ping', 'En ruta'),
        ('llegada', 'Llegada'),
        ('firma', 'Firma'),
        ('fin', 'Fin de ruta'),
    ], string='Evento', required=True, default='ping', index=True)
    latitude = fields.Float(string='Latitud', digits=(10, 7), required=True)
    longitude = fields.Float(string='Longitud', digits=(10, 7), required=True)
    accuracy = fields.Float(string='Precisión (m)')
    speed = fields.Float(string='Velocidad (m/s)')
    timestamp = fields.Datetime(string='Momento', required=True,
                                default=fields.Datetime.now, index=True)

    @api.model
    def app_log_points(self, points):
        """Alta masiva desde la app (cola offline). points = lista de dicts
        con document_id, event_type, latitude, longitude, accuracy, speed,
        timestamp (ISO UTC)."""
        vals_list = []
        for p in points or []:
            if not p.get('document_id') or not p.get('latitude'):
                continue
            ts = (p.get('timestamp') or '').replace('T', ' ').split('.')[0]
            vals_list.append({
                'document_id': p['document_id'],
                'event_type': p.get('event_type', 'ping'),
                'latitude': p['latitude'],
                'longitude': p['longitude'],
                'accuracy': p.get('accuracy') or 0.0,
                'speed': p.get('speed') or 0.0,
                'timestamp': ts or fields.Datetime.now(),
            })
        if vals_list:
            self.sudo().create(vals_list)
        return {'ok': True, 'logged': len(vals_list)}


class SaleDeliveryLiveMap(models.TransientModel):
    """Servicio de datos del mapa de entregas.

    La UI es una client action OWL (delivery_live_map.js) con Leaflet
    vendorizado. Este modelo expone get_route_data() con métricas por
    viaje: distancia real (haversine), velocidad promedio y pico (km/h),
    tiempo detenido, PT/OV/cliente y materiales entregados.
    """
    _name = 'sale.delivery.live.map'
    _description = 'Mapa de Entregas'

    @staticmethod
    def _hav_m(lat1, lon1, lat2, lon2):
        """Distancia haversine en metros."""
        rlat1, rlon1, rlat2, rlon2 = map(
            math.radians, (lat1, lon1, lat2, lon2))
        h = (math.sin((rlat2 - rlat1) / 2) ** 2
             + math.cos(rlat1) * math.cos(rlat2)
             * math.sin((rlon2 - rlon1) / 2) ** 2)
        return 2 * 6371000.0 * math.asin(math.sqrt(h))

    @api.model
    def get_route_data(self, mode='live'):
        """Rutas para el mapa.

        mode='live':    TODOS los viajes del día (zona horaria del usuario),
                        actualizándose en tiempo real hasta terminar.
        mode='history': todas las rutas registradas.
        """
        Point = self.env['sale.delivery.route.point'].sudo()
        domain = []
        if mode != 'history':
            # Desde la medianoche del usuario: todos los viajes de HOY.
            tz = pytz.timezone(self.env.user.tz or 'America/Monterrey')
            today = fields.Date.context_today(self)
            start_local = tz.localize(datetime.combine(today, dtime.min))
            since = start_local.astimezone(pytz.utc).replace(tzinfo=None)
            domain = [('timestamp', '>=', since)]
        points = Point.search(domain, order='timestamp asc', limit=20000)

        routes = {}
        for pt in points:
            routes.setdefault(pt.document_id.id, []).append(pt)

        colors = ['#0B57D0', '#00B894', '#E5484D', '#F5A623', '#7C3AED',
                  '#0891B2', '#DB2777']

        def fmt(ts):
            return fields.Datetime.context_timestamp(
                self, ts).strftime('%d/%m %H:%M')

        result = []
        for idx, (doc_id, pts) in enumerate(routes.items()):
            doc = self.env['sale.delivery.document'].sudo().browse(doc_id)
            last = pts[-1]
            finished = any(p.event_type == 'fin' for p in pts)

            # ── Métricas del viaje ──────────────────────────────────
            # Distancia y velocidades por segmentos GPS consecutivos.
            # Umbral 3 km/h y 8 m: por debajo se considera DETENIDO (el
            # GPS "respira" parado y sumaría metros/velocidades falsas).
            total_m = 0.0
            moving_s = 0.0
            stopped_s = 0.0
            peak_kmh = 0.0
            kept = []          # polyline simplificada (>=12 m entre puntos)
            last_kept = None
            prev = None
            for p in pts:
                if prev is not None:
                    dt = (p.timestamp - prev.timestamp).total_seconds()
                    if 0 < dt <= 900:
                        dm = self._hav_m(prev.latitude, prev.longitude,
                                         p.latitude, p.longitude)
                        kmh = (dm / dt) * 3.6
                        if kmh <= 160:  # descarta saltos GPS irreales
                            if kmh >= 3.0 and dm >= 8.0:
                                total_m += dm
                                moving_s += dt
                                peak_kmh = max(peak_kmh, kmh)
                            else:
                                stopped_s += dt
                    rec_kmh = (p.speed or 0.0) * 3.6
                    if 3.0 <= rec_kmh <= 160.0:
                        peak_kmh = max(peak_kmh, rec_kmh)
                if (last_kept is None
                        or p.event_type != 'ping'
                        or self._hav_m(last_kept[0], last_kept[1],
                                       p.latitude, p.longitude) >= 12.0):
                    kept.append([p.latitude, p.longitude])
                    last_kept = (p.latitude, p.longitude)
                prev = p
            end_ll = [pts[-1].latitude, pts[-1].longitude]
            if not kept or kept[-1] != end_ll:
                kept.append(end_ll)

            avg_kmh = (total_m / moving_s) * 3.6 if moving_s else 0.0
            duration_s = (pts[-1].timestamp - pts[0].timestamp).total_seconds()

            # ── Qué se entregó (desde el documento) ─────────────────
            mats = {}
            for line in doc.line_ids:
                if not line.product_id:
                    continue
                key = line.product_id.display_name
                mats[key] = mats.get(key, 0.0) + (line.qty_selected or 0.0)
            materials = [
                {'product': k, 'qty': round(v, 2)}
                for k, v in list(mats.items())[:25]
            ]

            driver = doc.vehicle_driver_id.name or last.user_id.name or ''
            result.append({
                'id': doc_id,
                'label': '%s · %s' % (
                    doc.remission_number or doc.name or '', driver),
                'doc_name': doc.remission_number or doc.name or '',
                'pt_folio': doc.pick_ticket_id.name or '',
                'sale_order': doc.sale_order_id.name or '',
                'partner': doc.partner_id.name or '',
                'driver': driver,
                'vehicle': doc.vehicle_id.display_name if doc.vehicle_id else '',
                'color': colors[idx % len(colors)],
                'finished': finished,
                'latlngs': kept,
                'distance_km': round(total_m / 1000.0, 1),
                'avg_kmh': round(avg_kmh),
                'peak_kmh': round(peak_kmh),
                'stopped_min': round(stopped_s / 60.0),
                'duration_min': round(duration_s / 60.0),
                'start_time': fmt(pts[0].timestamp),
                'materials': materials,
                'last': {
                    'lat': last.latitude,
                    'lng': last.longitude,
                    'time': fmt(last.timestamp),
                },
                'events': [{
                    'lat': p.latitude,
                    'lng': p.longitude,
                    'type': p.event_type,
                    'time': fmt(p.timestamp),
                } for p in pts if p.event_type in (
                    'inicio', 'llegada', 'firma', 'fin')],
            })
        return {'mode': mode, 'routes': result}

    # ══════════════════════════════════════════════════════════════
    # REPORTERÍA — dashboard de operación de entregas
    # ══════════════════════════════════════════════════════════════

    def _trip_stats(self, pts):
        """Métricas GPS de un viaje: (dist_m, moving_s, stopped_s, peak_kmh).
        Mismos umbrales que el mapa (3 km/h / 8 m anti-jitter, tope 160)."""
        total_m = moving_s = stopped_s = peak = 0.0
        prev = None
        for p in pts:
            if prev is not None:
                dt = (p.timestamp - prev.timestamp).total_seconds()
                if 0 < dt <= 900:
                    dm = self._hav_m(prev.latitude, prev.longitude,
                                     p.latitude, p.longitude)
                    kmh = (dm / dt) * 3.6
                    if kmh <= 160:
                        if kmh >= 3.0 and dm >= 8.0:
                            total_m += dm
                            moving_s += dt
                            peak = max(peak, kmh)
                        else:
                            stopped_s += dt
                rec = (p.speed or 0.0) * 3.6
                if 3.0 <= rec <= 160.0:
                    peak = max(peak, rec)
            prev = p
        return total_m, moving_s, stopped_s, peak

    @api.model
    def get_report_data(self, days=30):
        """Agregados para el dashboard de Reportería (rango: hoy - days+1)."""
        days = max(1, min(int(days or 30), 365))
        tz = pytz.timezone(self.env.user.tz or 'America/Monterrey')
        today = fields.Date.context_today(self)
        from datetime import timedelta
        start_date = today - timedelta(days=days - 1)
        start_local = tz.localize(datetime.combine(start_date, dtime.min))
        since = start_local.astimezone(pytz.utc).replace(tzinfo=None)

        Doc = self.env['sale.delivery.document'].sudo()

        def doc_m2(doc):
            total = 0.0
            for line in doc.line_ids:
                total += line.qty_done or line.qty_selected or 0.0
            return total

        remissions = Doc.search([
            ('document_type', '=', 'remission'),
            ('state', '!=', 'cancelled'),
            ('create_date', '>=', since),
        ])
        returns = Doc.search([
            ('document_type', '=', 'return'),
            ('state', '!=', 'cancelled'),
            ('create_date', '>=', since),
        ])

        # ── Métricas GPS por documento del periodo ──────────────
        Point = self.env['sale.delivery.route.point'].sudo()
        points = Point.search([('timestamp', '>=', since)],
                              order='timestamp asc', limit=100000)
        pts_by_doc = {}
        for p in points:
            pts_by_doc.setdefault(p.document_id.id, []).append(p)
        gps = {}
        for doc_id, pts in pts_by_doc.items():
            dist_m, moving_s, stopped_s, peak = self._trip_stats(pts)
            gps[doc_id] = {
                'km': dist_m / 1000.0,
                'moving_s': moving_s,
                'stopped_s': stopped_s,
                'peak': peak,
                'duration_s': (pts[-1].timestamp - pts[0].timestamp).total_seconds(),
            }

        # ── Serie diaria (todas las fechas del rango, con ceros) ──
        daily = {}
        d = start_date
        while d <= today:
            daily[d.isoformat()] = {'date': d.strftime('%d/%m'), 'trips': 0, 'm2': 0.0}
            d += timedelta(days=1)

        # ── Agregación por vehículo / chofer / cliente ──────────
        vehicles = {}
        drivers = {}
        customers = {}
        total_m2 = 0.0
        util_samples = []

        for doc in remissions:
            m2 = doc_m2(doc)
            total_m2 += m2
            local_day = fields.Datetime.context_timestamp(
                self, doc.create_date).date().isoformat()
            if local_day in daily:
                daily[local_day]['trips'] += 1
                daily[local_day]['m2'] += m2

            g = gps.get(doc.id, {})

            veh = doc.vehicle_id
            vkey = veh.id or 0
            v = vehicles.setdefault(vkey, {
                'name': veh.display_name if veh else 'Sin vehículo',
                'capacity': veh.x_capacity_sqm if veh else 0.0,
                'odometer': veh.odometer if veh else 0.0,
                'trips': 0, 'm2': 0.0, 'km': 0.0,
                'util': [], 'stopped_s': 0.0, 'moving_s': 0.0,
            })
            v['trips'] += 1
            v['m2'] += m2
            v['km'] += g.get('km', 0.0)
            v['stopped_s'] += g.get('stopped_s', 0.0)
            v['moving_s'] += g.get('moving_s', 0.0)
            if v['capacity'] and m2:
                pct = (m2 / v['capacity']) * 100.0
                v['util'].append(pct)
                util_samples.append(pct)

            drv = doc.vehicle_driver_id
            dkey = drv.id or 0
            dr = drivers.setdefault(dkey, {
                'name': drv.display_name if drv else 'Sin chofer',
                'trips': 0, 'm2': 0.0, 'km': 0.0,
                'moving_s': 0.0, 'stopped_s': 0.0, 'peak': 0.0,
            })
            dr['trips'] += 1
            dr['m2'] += m2
            dr['km'] += g.get('km', 0.0)
            dr['moving_s'] += g.get('moving_s', 0.0)
            dr['stopped_s'] += g.get('stopped_s', 0.0)
            dr['peak'] = max(dr['peak'], g.get('peak', 0.0))

            if doc.partner_id:
                c = customers.setdefault(doc.partner_id.id, {
                    'name': doc.partner_id.name, 'm2': 0.0, 'trips': 0})
                c['m2'] += m2
                c['trips'] += 1

        # ── Odómetro del periodo (módulo de flota) ──────────────
        veh_ids = [k for k in vehicles if k]
        if veh_ids:
            odo_recs = self.env['fleet.vehicle.odometer'].sudo().search([
                ('vehicle_id', 'in', veh_ids),
                ('date', '>=', start_date),
            ])
            odo_by_veh = {}
            for rec in odo_recs:
                odo_by_veh.setdefault(rec.vehicle_id.id, []).append(rec.value)
            for vid, values in odo_by_veh.items():
                if len(values) >= 2:
                    vehicles[vid]['odo_period_km'] = round(
                        max(values) - min(values), 1)

        # ── Devoluciones por motivo ─────────────────────────────
        reasons = {}
        returned_m2 = 0.0
        for doc in returns:
            m2 = doc_m2(doc)
            returned_m2 += m2
            name = doc.return_reason_id.name or 'Sin motivo'
            r = reasons.setdefault(name, {'reason': name, 'count': 0, 'm2': 0.0})
            r['count'] += 1
            r['m2'] += m2

        # ── Salida ──────────────────────────────────────────────
        def _veh_out(v):
            avg_util = sum(v['util']) / len(v['util']) if v['util'] else 0.0
            return {
                'name': v['name'],
                'capacity': round(v['capacity'], 1),
                'trips': v['trips'],
                'm2': round(v['m2'], 1),
                'km': round(v['km'], 1),
                'avg_util': round(avg_util),
                'odometer': round(v['odometer']),
                'odo_period_km': v.get('odo_period_km', 0),
                'avg_kmh': round((v['km'] * 1000 / v['moving_s']) * 3.6) if v['moving_s'] else 0,
                'stopped_min': round(v['stopped_s'] / 60.0),
            }

        def _drv_out(d):
            return {
                'name': d['name'],
                'trips': d['trips'],
                'm2': round(d['m2'], 1),
                'km': round(d['km'], 1),
                'avg_kmh': round((d['km'] * 1000 / d['moving_s']) * 3.6) if d['moving_s'] else 0,
                'peak_kmh': round(d['peak']),
                'stopped_min': round(d['stopped_s'] / 60.0),
            }

        total_km = sum(v['km'] for v in vehicles.values())
        total_moving = sum(v['moving_s'] for v in vehicles.values())
        total_stopped = sum(v['stopped_s'] for v in vehicles.values())
        trips = len(remissions)

        return {
            'days': days,
            'kpis': {
                'trips': trips,
                'm2': round(total_m2, 1),
                'km': round(total_km, 1),
                'avg_util': round(sum(util_samples) / len(util_samples)) if util_samples else 0,
                'returns': len(returns),
                'returned_m2': round(returned_m2, 1),
                'return_rate': round(len(returns) * 100.0 / trips) if trips else 0,
                'avg_kmh': round((total_km * 1000 / total_moving) * 3.6) if total_moving else 0,
                'stopped_min': round(total_stopped / 60.0),
                'm2_per_km': round(total_m2 / total_km, 1) if total_km else 0,
            },
            'daily': list(daily.values()),
            'vehicles': sorted((_veh_out(v) for v in vehicles.values()),
                               key=lambda x: -x['m2']),
            'drivers': sorted((_drv_out(d) for d in drivers.values()),
                              key=lambda x: -x['m2']),
            'returns': sorted(reasons.values(), key=lambda x: -x['count']),
            'customers': sorted(customers.values(), key=lambda x: -x['m2'])[:10],
        }

    # ══════════════════════════════════════════════════════════════
    # SALIDAS — tablero del día (pick tickets → remisiones → firmas)
    # ══════════════════════════════════════════════════════════════

    @api.model
    def get_outbound_dashboard_data(self):
        """Todo lo que el almacén trabaja HOY: pick tickets abiertos
        (la orden del día), remisiones en ruta, entregas firmadas y la
        carga asignada a cada camión contra su capacidad en m²."""
        from datetime import timedelta as _td
        tz = pytz.timezone(self.env.user.tz or 'America/Monterrey')
        today = fields.Date.context_today(self)
        start_local = tz.localize(datetime.combine(today, dtime.min))
        today_start = start_local.astimezone(pytz.utc).replace(tzinfo=None)

        Doc = self.env['sale.delivery.document'].sudo()

        def doc_m2(doc):
            return sum(
                (l.qty_done or l.qty_selected or 0.0) for l in doc.line_ids)

        def fmt_dt(ts):
            if not ts:
                return ''
            return fields.Datetime.context_timestamp(
                self, ts).strftime('%d/%m %H:%M')

        def base_card(doc):
            order = doc.sale_order_id
            auth = getattr(order, 'delivery_auth_state', '') or ''
            return {
                'id': doc.id,
                'name': doc.name or '',
                'order': order.name or '',
                'order_id': order.id or False,
                'partner': doc.partner_id.name or '',
                'vehicle': doc.vehicle_id.display_name if doc.vehicle_id else '',
                'driver': doc.vehicle_driver_id.display_name
                          if doc.vehicle_driver_id else '',
                'm2': round(doc_m2(doc), 1),
                'lines': len(doc.line_ids),
                'created': fmt_dt(doc.create_date),
                'is_today': bool(doc.create_date and doc.create_date >= today_start),
                'auth': auth,
                'auth_ok': auth in ('authorized', 'paid'),
            }

        # ── Pick tickets abiertos (backlog completo, no solo hoy) ──
        open_pts = Doc.search([
            ('document_type', '=', 'pick_ticket'),
            ('state', 'in', ('draft', 'prepared')),
        ], order='create_date asc')

        pending, ready = [], []
        for pt in open_pts:
            card = base_card(pt)
            card['state'] = pt.state
            (ready if pt.state == 'prepared' else pending).append(card)

        # ── Remisiones de hoy: en ruta vs entregadas (firmadas) ──
        remissions_today = Doc.search([
            ('document_type', '=', 'remission'),
            ('state', '!=', 'cancelled'),
            ('create_date', '>=', today_start),
        ], order='create_date desc')

        in_route, delivered = [], []
        for rem in remissions_today:
            card = base_card(rem)
            card['name'] = rem.remission_number or rem.name or ''
            card['pt'] = rem.pick_ticket_id.name or ''
            card['signed_at'] = fmt_dt(rem.signed_at)
            if rem.signed_at:
                delivered.append(card)
            else:
                in_route.append(card)

        # ── Carga por camión (PTs abiertos + remisiones de hoy) ──
        trucks = {}
        for doc in list(open_pts) + list(remissions_today):
            veh = doc.vehicle_id
            if not veh:
                continue
            t = trucks.setdefault(veh.id, {
                'name': veh.display_name,
                'driver': '',
                'capacity': round(getattr(veh, 'x_capacity_sqm', 0.0) or 0.0, 1),
                'm2': 0.0,
                'docs': [],
            })
            if doc.vehicle_driver_id and not t['driver']:
                t['driver'] = doc.vehicle_driver_id.display_name
            t['m2'] += doc_m2(doc)
            label = (doc.remission_number or doc.name or '')
            if doc.document_type == 'pick_ticket':
                status = 'PT ' + ('listo' if doc.state == 'prepared' else 'pendiente')
            else:
                status = 'Entregada' if doc.signed_at else 'En ruta'
            t['docs'].append({
                'id': doc.id,
                'label': label,
                'status': status,
                'm2': round(doc_m2(doc), 1),
            })
        truck_list = []
        for t in trucks.values():
            t['m2'] = round(t['m2'], 1)
            t['pct'] = round(t['m2'] * 100.0 / t['capacity']) if t['capacity'] else 0
            truck_list.append(t)
        truck_list.sort(key=lambda x: -x['m2'])

        pts_today = [c for c in pending + ready if c['is_today']]
        no_vehicle = [c for c in pending + ready if not c['vehicle']]

        return {
            'kpis': {
                'pts_today': len(pts_today),
                'pts_open': len(pending) + len(ready),
                'ready': len(ready),
                'm2_to_deliver': round(
                    sum(c['m2'] for c in pending + ready), 1),
                'in_route': len(in_route),
                'delivered': len(delivered),
                'delivered_m2': round(sum(c['m2'] for c in delivered), 1),
                'no_vehicle': len(no_vehicle),
                'trucks': len(truck_list),
            },
            'pending': pending,
            'ready': ready,
            'in_route': in_route,
            'delivered': delivered,
            'trucks': truck_list,
        }

