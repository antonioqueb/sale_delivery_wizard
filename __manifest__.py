{
    'name': 'Sale Delivery Wizard - SOM',
    'version': '19.0.1.0.0',
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
    """,
    'author': 'Alphaqueb Consulting SAS',
    'website': 'https://alphaqueb.com',
    'depends': [
        'sale_management',
        'stock',
        'sale_stock',
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
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
