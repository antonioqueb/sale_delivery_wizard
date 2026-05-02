from odoo.tests import tagged
from odoo.exceptions import UserError

from .common import SaleDeliveryWizardTestCommon


@tagged("post_install", "-at_install", "sdw_pick_ticket")
class TestPickTicketMultiFlow(SaleDeliveryWizardTestCommon):

    def test_one_open_pick_ticket_opens_selector_and_allows_new_pt(self):
        product = self._create_product("SDW Multi PT Product")
        lot_a = self._create_lot(product, "SDW-MULTI-A")
        lot_b = self._create_lot(product, "SDW-MULTI-B")

        self._set_lot_stock(product, lot_a, 1.0)
        self._set_lot_stock(product, lot_b, 1.0)

        order, sale_line = self._create_sale_order(
            product=product,
            qty=2.0,
            lots=[lot_a, lot_b],
            state="sale",
        )

        picking_a, move_a, ml_a = self._create_pending_picking_with_lot(
            order, sale_line, product, lot_a, qty=1.0
        )
        self._create_pending_picking_with_lot(
            order, sale_line, product, lot_b, qty=1.0
        )

        pt = self._create_prepared_pick_ticket(
            order=order,
            sale_line=sale_line,
            product=product,
            lot=lot_a,
            qty=1.0,
            move=move_a,
            move_line=ml_a,
        )

        action = order.action_open_delivery_wizard()
        self.assertNotIn(
            "default_editing_pick_ticket_id",
            action.get("context", {}),
            "Con un solo PT abierto no debe forzar edición automática; debe mostrar selector.",
        )

        wizard = self.env["sale.delivery.wizard"].with_context(action["context"]).create({})
        self.assertEqual(wizard.wizard_state, "select_pt")
        self.assertIn(pt, wizard.open_pt_ids)

        wizard.action_start_new_pt()
        self.assertEqual(wizard.wizard_state, "select")

        data_lines = wizard.line_ids.filtered(lambda line: line.display_type != "line_section")
        available_lots = set(data_lines.mapped("lot_id").ids)

        self.assertNotIn(
            lot_a.id,
            available_lots,
            "El lote del PT abierto debe quedar bloqueado para un nuevo PT.",
        )
        self.assertIn(
            lot_b.id,
            available_lots,
            "El nuevo PT debe poder seleccionar lotes aún disponibles.",
        )

    def test_lot_collision_blocks_duplicate_pt_but_allows_same_pt_edit(self):
        product = self._create_product("SDW Collision Product")
        lot = self._create_lot(product, "SDW-COLLISION-A")
        self._set_lot_stock(product, lot, 1.0)

        order, sale_line = self._create_sale_order(
            product=product,
            qty=1.0,
            lots=[lot],
            state="sale",
        )
        picking, move, ml = self._create_pending_picking_with_lot(
            order, sale_line, product, lot, qty=1.0
        )

        pt = self._create_prepared_pick_ticket(
            order=order,
            sale_line=sale_line,
            product=product,
            lot=lot,
            qty=1.0,
            move=move,
            move_line=ml,
        )

        wizard = self.env["sale.delivery.wizard"].create({"sale_order_id": order.id})
        selections = [{
            "lotId": lot.id,
            "productId": product.id,
            "qty": 1.0,
            "pickingId": picking.id,
            "moveId": move.id,
            "moveLineId": ml.id,
            "saleLineId": sale_line.id,
        }]

        with self.assertRaises(UserError):
            wizard._validate_no_lot_collision(selections)

        # Editing the same PT must exclude itself and therefore not collide.
        wizard._validate_no_lot_collision(selections, exclude_pt_id=pt.id)

    def test_cancel_pick_ticket_releases_locked_lots(self):
        product = self._create_product("SDW Cancel PT Product")
        lot = self._create_lot(product, "SDW-CANCEL-PT-A")
        self._set_lot_stock(product, lot, 1.0)

        order, sale_line = self._create_sale_order(
            product=product,
            qty=1.0,
            lots=[lot],
            state="sale",
        )
        picking, move, ml = self._create_pending_picking_with_lot(
            order, sale_line, product, lot, qty=1.0
        )

        pt = self._create_prepared_pick_ticket(
            order=order,
            sale_line=sale_line,
            product=product,
            lot=lot,
            qty=1.0,
            move=move,
            move_line=ml,
        )

        self.assertIn(lot.id, order._get_locked_lot_ids())

        pt.action_cancel_pick_ticket()

        self.assertEqual(pt.state, "cancelled")
        self.assertNotIn(lot.id, order._get_locked_lot_ids())
