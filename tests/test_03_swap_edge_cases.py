from odoo.tests import tagged
from odoo.exceptions import UserError

from .common import SaleDeliveryWizardTestCommon


@tagged("post_install", "-at_install", "sdw_swap_unit")
class TestSwapEdgeCases(SaleDeliveryWizardTestCommon):

    def test_swap_without_selection_raises(self):
        fx = self._create_base_swap_fixture(qty=1.0)
        wizard = self._create_swap_wizard(fx["order"])
        wizard.write({"widget_selections": "[]"})

        with self.assertRaises(UserError):
            wizard.action_confirm_swap()

    def test_swap_same_origin_and_target_raises(self):
        fx = self._create_base_swap_fixture(qty=1.0)
        wizard = self._create_swap_wizard(fx["order"])
        wizard.write({
            "widget_selections": self._swap_payload(
                product=fx["product"],
                origin_lot=fx["lot_origin"],
                target_lot=fx["lot_origin"],
                move_line=fx["move_line"],
                sale_line=fx["sale_line"],
                picking=fx["picking"],
                qty=fx["qty"],
            )
        })

        with self.assertRaises(UserError):
            wizard.action_confirm_swap()

    def test_swap_target_without_stock_raises(self):
        fx = self._create_base_swap_fixture(qty=1.0)
        empty_target = self._create_lot(fx["product"], "SDW-NO-STOCK-TARGET")

        wizard = self._create_swap_wizard(fx["order"])
        wizard.write({
            "widget_selections": self._swap_payload(
                product=fx["product"],
                origin_lot=fx["lot_origin"],
                target_lot=empty_target,
                move_line=fx["move_line"],
                sale_line=fx["sale_line"],
                picking=fx["picking"],
                qty=fx["qty"],
            )
        })

        with self.assertRaises(UserError):
            wizard.action_confirm_swap()

    def test_swap_target_product_mismatch_raises(self):
        fx = self._create_base_swap_fixture(qty=1.0)
        other_product = self._create_product("SDW Other Product")
        wrong_target = self._create_lot(other_product, "SDW-WRONG-PRODUCT-TARGET")
        self._set_lot_stock(other_product, wrong_target, 1.0)

        wizard = self._create_swap_wizard(fx["order"])
        wizard.write({
            "widget_selections": self._swap_payload(
                product=fx["product"],
                origin_lot=fx["lot_origin"],
                target_lot=wrong_target,
                move_line=fx["move_line"],
                sale_line=fx["sale_line"],
                picking=fx["picking"],
                qty=fx["qty"],
            )
        })

        with self.assertRaises(UserError):
            wizard.action_confirm_swap()

    def test_swap_rejects_non_pending_move(self):
        fx = self._create_base_swap_fixture(qty=1.0)
        fx["move"].write({"state": "done"})

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

        with self.assertRaises(UserError):
            wizard.action_confirm_swap()
