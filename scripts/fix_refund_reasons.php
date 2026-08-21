<?php
/**
 * Align refund reasons with the product that was returned.
 *
 * The generator picked reasons at random, which produced nonsense such as a
 * hoodie returned for poor battery life. Those strings end up in the bot's
 * answer and in the Slack digest, so they have to make sense.
 *
 * Refunds cannot be updated through the REST API: WooCommerce treats them as
 * immutable by design. The reason is stored as _refund_reason in the HPOS meta
 * table, so it is rewritten there directly. Seeding-time workaround only.
 *
 * Classification reads the line items of the refund itself. Reading the parent
 * order instead would misclassify a returned beanie as electronics whenever the
 * original basket happened to contain a gadget.
 *
 * Run from the LocalWP site shell:
 *   wp eval-file <path>\fix_refund_reasons.php
 *   wp eval-file <path>\fix_refund_reasons.php apply
 */

global $wpdb;

$apply        = in_array( 'apply', $args, true );
$orders_tbl   = $wpdb->prefix . 'wc_orders';
$meta_tbl     = $wpdb->prefix . 'wc_orders_meta';
$items_tbl    = $wpdb->prefix . 'woocommerce_order_items';
$itemmeta_tbl = $wpdb->prefix . 'woocommerce_order_itemmeta';

mt_srand( 20260821 );

$reason_pools = array(
	'electronics' => array(
		'Battery life below expectations',
		'Faulty on arrival',
		'Difficult to pair with phone',
		'Sound quality below expectations',
		'Arrived damaged',
		'Not compatible with my device',
	),
	'apparel'     => array(
		'Wrong size',
		'Colour differs from the photos',
		'Fabric quality below expectations',
		'Customer changed their mind',
		'Ordered two sizes, keeping one',
		'Arrived damaged',
	),
	'other'       => array(
		'Customer changed their mind',
		'Arrived damaged',
		'Item did not match the description',
		'Better price found elsewhere',
	),
);

/**
 * Return the product ids attached to a given order or refund.
 */
function seed_product_ids( $order_id ) {
	global $wpdb;

	return $wpdb->get_col(
		$wpdb->prepare(
			'SELECT im.meta_value
			 FROM ' . $wpdb->prefix . 'woocommerce_order_items i
			 JOIN ' . $wpdb->prefix . 'woocommerce_order_itemmeta im
			   ON im.order_item_id = i.order_item_id
			 WHERE i.order_id = %d
			   AND i.order_item_type = %s
			   AND im.meta_key = %s',
			$order_id,
			'line_item',
			'_product_id'
		)
	);
}

/**
 * Decide which reason pool fits the returned products.
 */
function seed_classify( array $product_ids ) {
	foreach ( $product_ids as $product_id ) {
		$product_id = (int) $product_id;
		$sku        = get_post_meta( $product_id, '_sku', true );

		if ( is_string( $sku ) && 0 === strpos( $sku, 'ELEC-' ) ) {
			return 'electronics';
		}

		if ( has_term( array( 'music', 'decor' ), 'product_cat', $product_id ) ) {
			return 'other';
		}
	}

	return empty( $product_ids ) ? 'other' : 'apparel';
}

$refunds = $wpdb->get_results(
	"SELECT id, parent_order_id FROM {$orders_tbl} WHERE type = 'shop_order_refund' ORDER BY id ASC"
);

if ( empty( $refunds ) ) {
	WP_CLI::error( 'No refunds found.' );
}

$updated = 0;
$counts  = array(
	'electronics' => 0,
	'apparel'     => 0,
	'other'       => 0,
);

foreach ( $refunds as $refund ) {
	$product_ids = seed_product_ids( $refund->id );
	$source      = 'refund';

	if ( empty( $product_ids ) ) {
		$product_ids = seed_product_ids( $refund->parent_order_id );
		$source      = 'parent';
	}

	$kind   = seed_classify( $product_ids );
	$pool   = $reason_pools[ $kind ];
	$reason = $pool[ mt_rand( 0, count( $pool ) - 1 ) ];

	$counts[ $kind ]++;

	$names = array();
	foreach ( $product_ids as $product_id ) {
		$names[] = get_the_title( (int) $product_id );
	}
	$label = implode( ', ', $names );

	WP_CLI::log(
		sprintf(
			'refund=%-6d %-6s %-12s %-34s -> %s',
			$refund->id,
			$source,
			$kind,
			substr( $label, 0, 34 ),
			$reason
		)
	);

	if ( $apply ) {
		$wpdb->update(
			$meta_tbl,
			array( 'meta_value' => $reason ),
			array(
				'order_id' => $refund->id,
				'meta_key' => '_refund_reason',
			),
			array( '%s' ),
			array( '%d', '%s' )
		);
		wp_cache_delete( (int) $refund->id, 'orders' );
		wp_cache_delete( (int) $refund->parent_order_id, 'orders' );
	}

	$updated++;
}

if ( $apply ) {
	wp_cache_flush();
}

WP_CLI::log( '' );
WP_CLI::log( $apply ? 'mode: APPLY' : 'mode: DRY-RUN' );
WP_CLI::log( "refunds processed: {$updated}" );
WP_CLI::log(
	sprintf(
		'electronics=%d apparel=%d other=%d',
		$counts['electronics'],
		$counts['apparel'],
		$counts['other']
	)
);

if ( ! $apply ) {
	WP_CLI::log( 'Nothing was written. Re-run with the "apply" argument to commit.' );
}