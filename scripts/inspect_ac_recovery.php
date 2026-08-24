<?php
/**
 * Recovery diagnostics for the Copilot abandoned carts plugin.
 *
 * Run from the LocalWP site shell:
 *   wp eval-file "C:\path\to\project-9\scripts\inspect_ac_recovery.php"
 *
 * Read-only. Lists the most recent orders, shows whether each one carries the
 * back-reference to a cart snapshot, and reports which payment gateways are
 * enabled, since a store with no enabled gateway cannot complete a checkout
 * at all and would make the recovery hooks look broken.
 *
 * @package CopilotAbandonedCarts
 */

if ( ! function_exists( 'copilot_ac_table_name' ) ) {
	echo "FAIL: plugin is not loaded.\n";
	return;
}

echo "Hook registered (classic)   : " . var_export( has_action( 'woocommerce_checkout_order_processed', 'copilot_ac_mark_recovered' ) !== false, true ) . "\n";
echo "Hook registered (store api) : " . var_export( has_action( 'woocommerce_store_api_checkout_order_processed', 'copilot_ac_mark_recovered_store_api' ) !== false, true ) . "\n\n";

echo "Enabled payment gateways:\n";
$gateways = WC()->payment_gateways() ? WC()->payment_gateways()->get_available_payment_gateways() : array();

if ( empty( $gateways ) ) {
	echo "  NONE. Checkout cannot be completed in a browser.\n";
} else {
	foreach ( $gateways as $gateway ) {
		printf( "  %-24s %s\n", $gateway->id, $gateway->get_title() );
	}
}

echo "\nCheckout page uses blocks   : ";
$checkout_page_id = (int) wc_get_page_id( 'checkout' );

if ( $checkout_page_id > 0 ) {
	$content = (string) get_post_field( 'post_content', $checkout_page_id );
	echo var_export( false !== strpos( $content, 'woocommerce/checkout' ), true ) . "\n";
} else {
	echo "checkout page not found\n";
}

echo "\nMost recent orders:\n";

$orders = wc_get_orders(
	array(
		'limit'   => 5,
		'orderby' => 'date',
		'order'   => 'DESC',
	)
);

if ( empty( $orders ) ) {
	echo "  none\n";
	return;
}

foreach ( $orders as $order ) {
	$snapshot_id = $order->get_meta( COPILOT_AC_ORDER_META_KEY );

	printf(
		"  #%-8s %-12s %-22s cart_snapshot=%s\n",
		$order->get_id(),
		$order->get_status(),
		$order->get_date_created() ? $order->get_date_created()->date( 'Y-m-d H:i:s' ) : 'no date',
		( '' === $snapshot_id ? '(none)' : $snapshot_id )
	);
}

echo "\nOK\n";