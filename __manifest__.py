{
    'name': 'Sale Delivery Wizard - SOM',
    'version': '19.0.1.3.0',
    'category': 'Sales/Delivery',
    'summary': 'Hub de entregas y devoluciones centralizado en la orden de venta',
    'description': """
        Módulo orquestador de entregas desde sale.order para Recubrimientos STO.
        - Wizard de entrega parcial con selección de lotes
        - Pick Ticket sin impacto de inventario
        - Remisión con impacto de inventario y secuencia propia
        - Swap de lotes previo a remisión
        - Devoluciones con motivo y resolución (Reagendar/Reponer/Finiquitar)
        - Fulfillment neto (entregado - devuelto)
        - Cockpit operativo en el formulario de venta
        - Vista agrupada por producto con acordeones colapsables
    """,
    'author': 'Alphaqueb Consulting SAS',
    'website': 'https://alphaqueb.com',
    'depends': [
        'sale_management',
        'stock',
        'sale_stock',
        'sale_stone_selection',
    ],
    'data': [
        'security/sale_delivery_groups.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'data/sale_return_reason_data.xml',
        'views/sale_order_views.xml',
        'views/sale_delivery_document_views.xml',
        'wizard/sale_delivery_wizard_views.xml',
        'wizard/sale_return_wizard_views.xml',
        'wizard/sale_swap_wizard_views.xml',
        'report/pick_ticket_report.xml',
        'report/remission_report.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'sale_delivery_wizard/static/src/scss/delivery_wizard.scss',
            'sale_delivery_wizard/static/src/scss/swap_lot_selector.scss',
            # Delivery Grouped List (collapsible accordion widget)
            'sale_delivery_wizard/static/src/components/delivery_grouped_list/delivery_grouped_list.scss',
            'sale_delivery_wizard/static/src/components/delivery_grouped_list/delivery_grouped_list.xml',
            'sale_delivery_wizard/static/src/components/delivery_grouped_list/delivery_grouped_list.js',
            # Swap Lot Selector (kept for backward compat, now also used inside grouped list)
            'sale_delivery_wizard/static/src/components/swap_lot_selector/swap_lot_selector.xml',
            'sale_delivery_wizard/static/src/components/swap_lot_selector/swap_lot_selector.js',
        ],
    },
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}