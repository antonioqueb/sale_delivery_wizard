from odoo.tests import tagged

from .common import SaleDeliveryWizardTestCommon


@tagged("post_install", "-at_install", "sdw_swap_unit")
class TestSwapFlow(SaleDeliveryWizardTestCommon):

    def test_swap_from_widget_json_replaces_move_line_sale_line_and_creates_audit_doc(self):
        fx = self._create_base_swap_fixture(qty=1.25)

        wizard = self._create_swap_wizard(fx["order"])
        wizard.write({
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

        action = wizard.action_confirm_swap()
        self.assertEqual(action.get("tag"), "display_notification")

        move_line = self.env["stock.move.line"].browse(fx["move_line"].id)
        self.assertEqual(move_line.lot_id, fx["lot_target"])
        self.assertEqual(move_line.location_id, fx["target_quant"].location_id)
        self.assertGreater(move_line.quantity, 0)

        if "lot_ids" in fx["sale_line"]._fields:
            self.assertIn(fx["lot_target"], fx["sale_line"].lot_ids)
            self.assertNotIn(fx["lot_origin"], fx["sale_line"].lot_ids)

        wizard = self.env["sale.swap.wizard"].browse(wizard.id)
        self.assertEqual(wizard.widget_selections, "[]")

        audit_docs = self.env["sale.delivery.document"].search([
            ("sale_order_id", "=", fx["order"].id),
            ("document_type", "=", "pick_ticket"),
            ("state", "=", "confirmed"),
        ])
        self.assertTrue(audit_docs, "El swap debe dejar documento de auditoría confirmado.")
        self.assertTrue(
            audit_docs.mapped("line_ids").filtered(lambda l: l.is_swap_origin and l.lot_id == fx["lot_origin"])
        )
        self.assertTrue(
            audit_docs.mapped("line_ids").filtered(lambda l: l.is_swap_target and l.lot_id == fx["lot_target"])
        )

    def test_swap_fallback_from_db_line_works_when_widget_json_is_empty(self):
        fx = self._create_base_swap_fixture(qty=1.0)

        wizard = self._create_swap_wizard(fx["order"])
        data_line = wizard.line_ids.filtered(
            lambda l: l.display_type != "line_section" and l.origin_lot_id == fx["lot_origin"]
        )[:1]
        self.assertTrue(data_line, "El wizard de swap debe contener la línea del lote origen.")

        data_line.write({"target_lot_id": fx["lot_target"].id})
        wizard.write({"widget_selections": "[]"})

        wizard.action_confirm_swap()

        move_line = self.env["stock.move.line"].browse(fx["move_line"].id)
        self.assertEqual(move_line.lot_id, fx["lot_target"])

    def test_delivery_grouped_data_shows_target_lot_after_swap_and_not_origin(self):
        fx = self._create_base_swap_fixture(qty=1.0)

        wizard = self._create_swap_wizard(fx["order"])
        wizard.write({
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
        wizard.action_confirm_swap()

        groups = fx["order"].get_delivery_grouped_data(mode="delivery")
        lot_ids = self._flatten_group_lot_ids(groups)

        self.assertIn(
            fx["lot_target"].id,
            lot_ids,
            "Después del swap, el packing list nuevo debe ver el lote destino.",
        )
        self.assertNotIn(
            fx["lot_origin"].id,
            lot_ids,
            "Después del swap, el packing list nuevo ya no debe ofrecer el lote origen.",
        )
