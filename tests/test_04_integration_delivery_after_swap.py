import json

from odoo.tests import tagged

from .common import SaleDeliveryWizardTestCommon


@tagged("post_install", "-at_install", "sdw_swap_integration")
class TestDeliveryAfterSwapIntegration(SaleDeliveryWizardTestCommon):

    def test_can_generate_new_pick_ticket_with_swapped_target_lot_only(self):
        fx = self._create_base_swap_fixture(qty=1.0)

        swap_wizard = self._create_swap_wizard(fx["order"])
        swap_wizard.write({
            "widget_selections": self._swap_payload(
                product=fx["product"],
                origin_lot=fx["lot_origin"],
                target_lot=fx["lot_target"],
                move_line=fx["move_line"],
                sale_line=fx["sale_line"],
                picking=fx["picking"],
                qty=fx["qty"],
            )
        })
        swap_wizard.action_confirm_swap()

        groups = fx["order"].get_delivery_grouped_data(mode="delivery")
        target_line = False

        for group in groups:
            for line in group.get("lines", []):
                if line.get("lotId") == fx["lot_target"].id:
                    target_line = line
                    break
            if target_line:
                break

        self.assertTrue(target_line, "El lote destino debe estar disponible para nuevo packing list.")
        self.assertNotIn(
            fx["lot_origin"].id,
            self._flatten_group_lot_ids(groups),
            "El lote origen ya no debe aparecer después del swap.",
        )

        delivery_wizard = self._create_delivery_wizard(fx["order"])
        delivery_wizard.write({
            "widget_selections": json.dumps([{
                "dbId": 0,
                "lotId": target_line.get("lotId"),
                "productId": target_line.get("productId"),
                "pickingId": target_line.get("pickingId"),
                "moveId": target_line.get("moveId"),
                "moveLineId": target_line.get("moveLineId"),
                "saleLineId": target_line.get("saleLineId"),
                "sourceLocationId": target_line.get("sourceLocationId"),
                "qty": target_line.get("qtyAvailable") or 1.0,
                "qtyAvailable": target_line.get("qtyAvailable") or 1.0,
            }])
        })

        delivery_wizard.action_generate_pick_ticket()
        pt = delivery_wizard.pick_ticket_id

        self.assertTrue(pt)
        self.assertEqual(pt.state, "prepared")
        self.assertEqual(pt.document_type, "pick_ticket")
        self.assertIn(fx["lot_target"], pt.line_ids.mapped("lot_id"))
        self.assertNotIn(fx["lot_origin"], pt.line_ids.mapped("lot_id"))

    def test_open_pick_ticket_selector_ignores_confirmed_swap_audit_docs(self):
        fx = self._create_base_swap_fixture(qty=1.0)

        swap_wizard = self._create_swap_wizard(fx["order"])
        swap_wizard.write({
            "widget_selections": self._swap_payload(
                product=fx["product"],
                origin_lot=fx["lot_origin"],
                target_lot=fx["lot_target"],
                move_line=fx["move_line"],
                sale_line=fx["sale_line"],
                picking=fx["picking"],
                qty=fx["qty"],
            )
        })
        swap_wizard.action_confirm_swap()

        # The swap audit document is a confirmed pick_ticket. It must not block
        # creation of a new PT because only prepared PTs are considered open.
        self.assertFalse(fx["order"]._get_open_pick_tickets())

        action = fx["order"].action_open_delivery_wizard()
        wizard = self.env["sale.delivery.wizard"].with_context(action["context"]).create({})

        self.assertEqual(
            wizard.wizard_state,
            "select",
            "Sin PTs prepared abiertos, el wizard debe ir directo a selección.",
        )
