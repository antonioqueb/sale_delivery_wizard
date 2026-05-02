from odoo.tests.common import TransactionCase
import json


class SaleDeliveryWizardTestCommon(TransactionCase):
    """Factories and assertions for sale_delivery_wizard tests.

    Estos helpers evitan depender de datos demo.
    Crean:
    - cliente
    - producto tipo Bienes / Consumible
    - lotes
    - quants
    - orden de venta
    - picking pendiente
    - move lines
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company

        cls.partner = cls.env["res.partner"].create({
            "name": "SDW Test Customer",
        })

        cls.uom = cls.env.ref("uom.product_uom_unit", raise_if_not_found=False)
        if not cls.uom:
            cls.uom = cls.env["uom.uom"].search([], limit=1)

        cls.warehouse = cls.env["stock.warehouse"].search([
            ("company_id", "=", cls.company.id),
        ], limit=1)
        if not cls.warehouse:
            cls.warehouse = cls.env["stock.warehouse"].search([], limit=1)

        cls.stock_location = (
            cls.warehouse.lot_stock_id
            or cls.env.ref("stock.stock_location_stock")
        )
        cls.customer_location = cls.env.ref("stock.stock_location_customers")

        cls.transit_location = cls.env["stock.location"].create({
            "name": "SDW Test Transit",
            "usage": "internal",
            "location_id": cls.stock_location.id,
            "company_id": cls.company.id,
        })

    # -------------------------------------------------------------------------
    # Generic compatibility helpers
    # -------------------------------------------------------------------------

    def _field_exists(self, model_name, field_name):
        return field_name in self.env[model_name]._fields

    def _selection_values(self, model_name, field_name):
        """Devuelve los valores técnicos disponibles de un campo selection."""
        field = self.env[model_name]._fields.get(field_name)
        if not field:
            return []

        selection = field.selection

        if callable(selection):
            try:
                selection = selection(self.env[model_name])
            except TypeError:
                selection = selection()

        return [value for value, _label in (selection or [])]

    def _first_valid_selection(self, model_name, field_name, preferred_values):
        """Elige el primer valor válido para el campo selection."""
        values = self._selection_values(model_name, field_name)

        for value in preferred_values:
            if value in values:
                return value

        return values[0] if values else False

    def _product_vals(self, name):
        ProductTemplate = self.env["product.template"]
        fields_map = ProductTemplate._fields

        vals = {
            "name": name,
            "uom_id": self.uom.id,
            "list_price": 100.0,
        }

        # Odoo 19 usa type='consu' para Bienes / Consumible.
        # Versiones anteriores podían usar detailed_type='product'.
        # Por eso se resuelve dinámicamente contra las opciones reales del campo.
        preferred_product_types = (
            "consu",      # Odoo 18/19: Bienes / Consumible
            "product",    # Odoo anteriores: Producto almacenable
            "storable",   # Compatibilidad con variantes/custom
            "goods",      # Compatibilidad con variantes/custom
            "service",
        )

        if "detailed_type" in fields_map:
            detailed_type = self._first_valid_selection(
                "product.template",
                "detailed_type",
                preferred_product_types,
            )
            if detailed_type:
                vals["detailed_type"] = detailed_type
        elif "type" in fields_map:
            product_type = self._first_valid_selection(
                "product.template",
                "type",
                preferred_product_types,
            )
            if product_type:
                vals["type"] = product_type

        # En algunas bases Odoo 18/19 separa "Bienes" de "Rastrear stock".
        # Solo se escribe si existe y es escribible en esa instalación.
        if "is_storable" in fields_map:
            field = fields_map["is_storable"]
            if not getattr(field, "readonly", False):
                vals["is_storable"] = True

        if "tracking" in fields_map:
            tracking = self._first_valid_selection(
                "product.template",
                "tracking",
                ("lot", "serial", "none"),
            )
            if tracking == "lot":
                vals["tracking"] = "lot"

        # Compatibilidad: en algunas versiones existe uom_po_id, en Odoo 19
        # puede no existir. Solo se manda si realmente está en el modelo.
        if "uom_po_id" in fields_map:
            vals["uom_po_id"] = self.uom.id

        return vals

    def _create_product(self, name="SDW Stone Product"):
        tmpl = self.env["product.template"].create(self._product_vals(name))
        return tmpl.product_variant_id

    def _create_lot(self, product, name, bloque="B-TEST", atado="A-TEST"):
        Lot = self.env["stock.lot"]

        vals = {
            "name": name,
            "product_id": product.id,
        }

        if "company_id" in Lot._fields:
            vals["company_id"] = self.company.id
        if "x_bloque" in Lot._fields:
            vals["x_bloque"] = bloque
        if "x_atado" in Lot._fields:
            vals["x_atado"] = atado
        if "x_alto" in Lot._fields:
            vals["x_alto"] = 240
        if "x_ancho" in Lot._fields:
            vals["x_ancho"] = 120
        if "x_grosor" in Lot._fields:
            vals["x_grosor"] = "2"
        if "x_tipo" in Lot._fields:
            vals["x_tipo"] = "placa"

        return Lot.create(vals)

    def _set_lot_stock(self, product, lot, qty=1.0, location=None):
        """Create stock for a product/lot.

        Prefer _update_available_quantity because it keeps stock.quant internals
        consistent. Fall back to direct quant creation for custom/older databases.
        """
        location = location or self.stock_location
        Quant = self.env["stock.quant"]

        try:
            Quant._update_available_quantity(product, location, qty, lot_id=lot)
        except TypeError:
            try:
                Quant._update_available_quantity(product, location, qty, lot_id=lot.id)
            except TypeError:
                try:
                    Quant._update_available_quantity(product, location, qty, lot)
                except Exception:
                    pass
        except Exception:
            pass

        quant = Quant.search([
            ("product_id", "=", product.id),
            ("lot_id", "=", lot.id),
            ("location_id", "=", location.id),
        ], limit=1)

        if not quant:
            vals = {
                "product_id": product.id,
                "lot_id": lot.id,
                "location_id": location.id,
                "quantity": qty,
            }
            if "company_id" in Quant._fields:
                vals["company_id"] = self.company.id
            quant = Quant.create(vals)

        if quant.quantity < qty:
            quant.sudo().write({"quantity": qty})

        return quant

    def _available_qty(self, quant):
        if "available_quantity" in quant._fields:
            return quant.available_quantity or 0.0
        return (quant.quantity or 0.0) - (quant.reserved_quantity or 0.0)

    def _create_sale_order(self, product, qty=1.0, lots=None, state="sale"):
        order_vals = {
            "partner_id": self.partner.id,
        }

        if "warehouse_id" in self.env["sale.order"]._fields and self.warehouse:
            order_vals["warehouse_id"] = self.warehouse.id

        order = self.env["sale.order"].create(order_vals)

        line_vals = {
            "order_id": order.id,
            "product_id": product.id,
            "product_uom_qty": qty,
            "product_uom": product.uom_id.id,
            "price_unit": 100.0,
        }
        line = self.env["sale.order.line"].create(line_vals)

        if lots and "lot_ids" in line._fields:
            line.write({"lot_ids": [(6, 0, [lot.id for lot in lots])]})

        if state:
            # Las pruebas se enfocan en la lógica de entrega/swap.
            # Escribir el estado directo evita ruido de confirmación estándar.
            order.write({"state": state})

        return order, line

    def _get_picking_type(self, code="internal"):
        if code == "outgoing":
            picking_type = self.warehouse.out_type_id
        elif code == "incoming":
            picking_type = self.warehouse.in_type_id
        else:
            picking_type = self.warehouse.int_type_id

        if not picking_type:
            picking_type = self.env["stock.picking.type"].search([
                ("code", "=", code),
            ], limit=1)

        return picking_type

    def _create_move_line(self, move, picking, product, lot, qty, source, dest):
        MoveLine = self.env["stock.move.line"]

        vals = {
            "move_id": move.id,
            "picking_id": picking.id,
            "product_id": product.id,
            "lot_id": lot.id,
            "quantity": qty,
            "location_id": source.id,
            "location_dest_id": dest.id,
        }

        if "product_uom_id" in MoveLine._fields:
            vals["product_uom_id"] = product.uom_id.id
        if "company_id" in MoveLine._fields:
            vals["company_id"] = self.company.id

        return MoveLine.create(vals)

    def _create_pending_picking_with_lot(
        self,
        order,
        sale_line,
        product,
        lot,
        qty=1.0,
        picking_type_code="internal",
        source_location=None,
        dest_location=None,
    ):
        source = source_location or self.stock_location
        dest = dest_location or (
            self.customer_location if picking_type_code == "outgoing" else self.transit_location
        )
        picking_type = self._get_picking_type(picking_type_code)

        picking_vals = {
            "picking_type_id": picking_type.id,
            "partner_id": order.partner_shipping_id.id or order.partner_id.id,
            "origin": order.name,
            "location_id": source.id,
            "location_dest_id": dest.id,
        }

        if "sale_id" in self.env["stock.picking"]._fields:
            picking_vals["sale_id"] = order.id
        if "company_id" in self.env["stock.picking"]._fields:
            picking_vals["company_id"] = self.company.id

        picking = self.env["stock.picking"].create(picking_vals)

        move_vals = {
            "name": product.display_name,
            "product_id": product.id,
            "product_uom_qty": qty,
            "product_uom": product.uom_id.id,
            "picking_id": picking.id,
            "location_id": source.id,
            "location_dest_id": dest.id,
            "origin": order.name,
        }

        if "sale_line_id" in self.env["stock.move"]._fields:
            move_vals["sale_line_id"] = sale_line.id
        if "company_id" in self.env["stock.move"]._fields:
            move_vals["company_id"] = self.company.id

        move = self.env["stock.move"].create(move_vals)

        # confirmed basta para la lógica custom del swap.
        picking.action_confirm()

        move_line = self._create_move_line(
            move=move,
            picking=picking,
            product=product,
            lot=lot,
            qty=qty,
            source=source,
            dest=dest,
        )

        return picking, move, move_line

    def _create_base_swap_fixture(self, qty=1.0):
        product = self._create_product("SDW Swap Product")
        lot_origin = self._create_lot(product, "SDW-ORIGIN-001", bloque="BO")
        lot_target = self._create_lot(product, "SDW-TARGET-001", bloque="BT")

        self._set_lot_stock(product, lot_origin, qty, self.stock_location)
        target_quant = self._set_lot_stock(
            product,
            lot_target,
            qty + 1.0,
            self.stock_location,
        )

        order, sale_line = self._create_sale_order(
            product=product,
            qty=qty,
            lots=[lot_origin],
            state="sale",
        )

        picking, move, move_line = self._create_pending_picking_with_lot(
            order=order,
            sale_line=sale_line,
            product=product,
            lot=lot_origin,
            qty=qty,
            picking_type_code="internal",
        )

        return {
            "product": product,
            "lot_origin": lot_origin,
            "lot_target": lot_target,
            "target_quant": target_quant,
            "order": order,
            "sale_line": sale_line,
            "picking": picking,
            "move": move,
            "move_line": move_line,
            "qty": qty,
        }

    def _swap_payload(self, product, origin_lot, target_lot, move_line, sale_line, picking, qty=1.0):
        return json.dumps([{
            "dbId": 0,
            "productId": product.id,
            "productName": product.display_name,
            "originLotId": origin_lot.id,
            "originLotName": origin_lot.name,
            "targetLotId": target_lot.id,
            "targetLotName": target_lot.name,
            "pickingId": picking.id,
            "moveLineId": move_line.id,
            "saleLineId": sale_line.id,
            "qty": qty,
            "targetQty": qty,
            "targetBloque": getattr(target_lot, "x_bloque", "") or "",
        }])

    def _create_swap_wizard(self, order):
        return self.env["sale.swap.wizard"].with_context(
            active_id=order.id,
            active_model="sale.order",
            default_sale_order_id=order.id,
        ).create({})

    def _create_delivery_wizard(self, order):
        return self.env["sale.delivery.wizard"].with_context(
            active_id=order.id,
            active_model="sale.order",
            default_sale_order_id=order.id,
        ).create({})

    def _create_prepared_pick_ticket(
        self,
        order,
        sale_line,
        product,
        lot,
        qty=1.0,
        move=None,
        move_line=None,
    ):
        doc = self.env["sale.delivery.document"].create({
            "document_type": "pick_ticket",
            "sale_order_id": order.id,
            "line_ids": [(0, 0, {
                "sale_line_id": sale_line.id,
                "move_id": move.id if move else False,
                "move_line_id": move_line.id if move_line else False,
                "product_id": product.id,
                "lot_id": lot.id,
                "qty_selected": qty,
                "source_location_id": self.stock_location.id,
            })],
        })
        doc.action_prepare()
        return doc

    def _flatten_group_lot_ids(self, groups):
        return {
            line.get("lotId") or line.get("originLotId") or 0
            for group in groups
            for line in group.get("lines", [])
        }