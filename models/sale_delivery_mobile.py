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
import json
import logging

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
            vals.update({
                'signature_image': payload['signature'],
                'signed_by': payload.get('signed_by') or '',
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
            template.send_mail(self.id, force_send=True)
            self.message_post(body=_(
                'Remisión firmada enviada por correo a %s.') % partner.email)
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
    """Mapa de entregas EN VIVO — Leaflet embebido que se alimenta solo:
    el HTML es un cascarón fijo y los datos llegan por polling JSON
    (get_route_data), sin botón de actualizar ni recargas de página.

    Dos modos (map_mode):
      - 'live':    última posición y ruta de las entregas activas (12 h).
      - 'history': todas las rutas registradas (submenú Rutas).
    """
    _name = 'sale.delivery.live.map'
    _description = 'Mapa de Entregas'

    map_html = fields.Html(string='Mapa', sanitize=False, readonly=True)
    active_count = fields.Integer(string='Entregas activas', readonly=True)
    map_mode = fields.Selection([
        ('live', 'En vivo'),
        ('history', 'Histórico'),
    ], string='Modo', default='live', readonly=True)

    @api.depends('map_mode')
    def _compute_display_name(self):
        # Sin esto, el breadcrumb mostraba el nombre técnico del transient
        # (sale.delivery.live.map,<id>).
        for rec in self:
            rec.display_name = (
                'Rutas' if rec.map_mode == 'history' else 'Mapa en Vivo')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        mode = self.env.context.get('default_map_mode') or 'live'
        res['map_mode'] = mode
        res['map_html'] = self._build_map_shell(mode)
        res['active_count'] = 0
        return res

    @api.model
    def get_route_data(self, mode='live'):
        """Datos para el mapa (lo llama el JS embebido por polling)."""
        Point = self.env['sale.delivery.route.point'].sudo()
        domain = []
        if mode != 'history':
            since = fields.Datetime.subtract(fields.Datetime.now(), hours=12)
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
            driver = doc.vehicle_driver_id.name or last.user_id.name or ''
            result.append({
                'id': doc_id,
                'label': '%s · %s' % (
                    doc.remission_number or doc.name or '', driver),
                'partner': doc.partner_id.name or '',
                'color': colors[idx % len(colors)],
                'finished': finished,
                'latlngs': [[p.latitude, p.longitude] for p in pts],
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

    @api.model
    def _build_map_shell(self, mode='live'):
        """Cascarón HTML fijo: mapa grande centrado en Monterrey. Los datos
        se pintan y refrescan solos vía get_route_data — nunca hay que
        regenerar este HTML."""
        map_id = 'delivery_map_%s' % mode
        # En vivo: refresco cada 15 s. Histórico: cada 60 s.
        interval = 15000 if mode == 'live' else 60000
        return """
<div style="width:100%%;height:calc(100vh - 165px);min-height:540px;position:relative;overflow:hidden;">
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <div id="%(map_id)s" style="width:100%%;height:100%%;"></div>
  <script>
    (function() {
      var el = document.getElementById('%(map_id)s');
      if (!el || el._leaflet_id) { return; }
      var MODE = '%(mode)s';
      var map = L.map('%(map_id)s', {scrollWheelZoom: true})
                 .setView([25.6866, -100.3161], 12);  /* Monterrey, MX */
      L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
          attribution: '&copy; OpenStreetMap &copy; CARTO', maxZoom: 19
      }).addTo(map);
      var layer = L.layerGroup().addTo(map);
      var fitted = false;
      var timer = null;
      function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function(c) {
          return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
        });
      }
      function draw(data) {
        layer.clearLayers();
        var bounds = [];
        (data.routes || []).forEach(function(r) {
          bounds = bounds.concat(r.latlngs);
          L.polyline(r.latlngs, {color: r.color, weight: 4, opacity: 0.75}).addTo(layer);
          var emoji = r.finished ? '\\ud83c\\udfc1' : '\\ud83d\\ude9a';
          L.marker([r.last.lat, r.last.lng], {icon: L.divIcon({
              html: '<div style="font-size:26px;filter:drop-shadow(0 1px 2px rgba(0,0,0,.4))">' + emoji + '</div>',
              className: '', iconSize: [26, 26]})})
            .addTo(layer)
            .bindPopup('<b>' + esc(r.label) + '</b><br/>' + esc(r.partner) +
                       '<br/>\\u00daltimo reporte: ' + esc(r.last.time) +
                       '<br/><a href="https://maps.google.com/?q=' + r.last.lat + ',' + r.last.lng +
                       '" target="_blank">Abrir en Google Maps</a>');
          (r.events || []).forEach(function(ev) {
            if (MODE === 'live' && (ev.type === 'inicio' || ev.type === 'fin')) { return; }
            var icons = {inicio: '\\ud83d\\udfe2', llegada: '\\ud83d\\udccd',
                         firma: '\\u270d\\ufe0f', fin: '\\ud83c\\udfc1'};
            L.marker([ev.lat, ev.lng], {icon: L.divIcon({
                html: '<div style="font-size:18px">' + (icons[ev.type] || '\\u2022') + '</div>',
                className: '', iconSize: [18, 18]})})
              .addTo(layer)
              .bindTooltip(esc(ev.type) + ' ' + esc(ev.time));
          });
        });
        if (!fitted && bounds.length) {
          map.fitBounds(bounds, {padding: [40, 40], maxZoom: 13});
          fitted = true;
        }
      }
      function load() {
        if (!document.getElementById('%(map_id)s')) {
          if (timer) { clearInterval(timer); }
          return;
        }
        fetch('/web/dataset/call_kw/sale.delivery.live.map/get_route_data', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({jsonrpc: '2.0', method: 'call', params: {
            model: 'sale.delivery.live.map', method: 'get_route_data',
            args: [MODE], kwargs: {}}})
        }).then(function(r) { return r.json(); })
          .then(function(res) { if (res && res.result) { draw(res.result); } })
          .catch(function() {});
      }
      load();
      timer = setInterval(load, %(interval)s);
    })();
  </script>
</div>""" % {'map_id': map_id, 'mode': mode, 'interval': interval}
