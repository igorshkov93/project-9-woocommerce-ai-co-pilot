<?php
/**
 * Seeds demo abandoned carts for the Copilot plugin.
 *
 * Run from the LocalWP site shell:
 *   wp eval-file "C:\path\to\project-9\scripts\seed_abandoned_carts.php"
 *   wp eval-file "C:\path\to\project-9\scripts\seed_abandoned_carts.php" apply
 *
 * Dry-run by default, consistent with every other seeding script in this
 * project. Only the "apply" argument writes anything.
 *
 * Rows are written straight into the snapshot table rather than through the
 * cart. Capture deliberately refuses to run under WP-CLI so that maintenance
 * scripts cannot pollute real shopper sessions, and the REST route is
 * read-only by design: opening it for writes would mean exposing customer
 * data to anything holding the credentials.
 *
 * Idempotent: every seeded row carries a session_key starting with the seed
 * prefix, so a re-run removes exactly the previous seed and nothing else.
 * Real carts captured from a browser are never touched.
 *
 * @package CopilotAbandonedCarts
 */

if ( ! function_exists( 'copilot_ac_table_name' ) ) {
	echo "FAIL: plugin is not loaded.\n";
	return;
}

global $wpdb;

$apply = in_array( 'apply', (array) $args, true );

$seed_prefix = 'seed-cart-';
$table       = copilot_ac_table_name();
$now_stamp   = time();

/**
 * Demo cart definitions.
 *
 * Times are expressed as minutes in the past relative to the moment the script
 * runs, so the data stays meaningful whenever it is re-seeded. The spread is
 * deliberate: it lets a demo show how threshold_minutes changes the result set
 * instead of returning the same rows for every threshold.
 */
$definitions = array(
	array(
		'slug'        => '01',
		'email'       => 'olena.k@example.com',
		'first_name'  => 'Olena',
		'minutes_ago' => 25,
		'items'       => array(
			array( 'sku' => 'ELEC-EARBUD-01', 'quantity' => 1 ),
		),
	),
	array(
		'slug'        => '02',
		'email'       => 'dmytro.b@example.com',
		'first_name'  => 'Dmytro',
		'minutes_ago' => 95,
		'items'       => array(
			array( 'sku' => 'ELEC-WATCH-01', 'quantity' => 1 ),
			array( 'sku' => 'ELEC-POWER-01', 'quantity' => 2 ),
		),
	),
	array(
		'slug'        => '03',
		'email'       => 'sofia.m@example.com',
		'first_name'  => 'Sofia',
		'minutes_ago' => 240,
		'items'       => array(
			array( 'sku' => 'ELEC-SPEAKER-01', 'quantity' => 1 ),
			array( 'sku' => 'woo-beanie', 'quantity' => 2 ),
		),
	),
	// No contact address: must be filtered out when require_email is true.
	// Without this row that parameter would never be exercised.
	array(
		'slug'        => '04',
		'email'       => '',
		'first_name'  => '',
		'minutes_ago' => 400,
		'items'       => array(
			array( 'sku' => 'ELEC-HUB-01', 'quantity' => 1 ),
		),
	),
	array(
		'slug'        => '05',
		'email'       => 'andrii.p@example.com',
		'first_name'  => 'Andrii',
		'minutes_ago' => 1500,
		'items'       => array(
			array( 'sku' => 'ELEC-MOUSE-01', 'quantity' => 3 ),
		),
	),
	array(
		'slug'        => '06',
		'email'       => 'kateryna.v@example.com',
		'first_name'  => 'Kateryna',
		'minutes_ago' => 3200,
		'items'       => array(
			array( 'sku' => 'ELEC-EARBUD-01', 'quantity' => 2 ),
			array( 'sku' => 'woo-cap', 'quantity' => 1 ),
		),
	),
	// Already converted: proves the recovery filter keeps the automation from
	// emailing someone who has already paid.
	array(
		'slug'        => '07',
		'email'       => 'maksym.t@example.com',
		'first_name'  => 'Maksym',
		'minutes_ago' => 600,
		'recovered'   => true,
		'items'       => array(
			array( 'sku' => 'ELEC-WATCH-01', 'quantity' => 1 ),
		),
	),
);

/**
 * Resolves a SKU into the frozen item shape used by the snapshot table.
 *
 * Fails loudly on an unknown SKU: silently seeding a cart with a missing
 * product would produce demo data that cannot be explained on camera.
 *
 * @param string $sku      Product SKU.
 * @param int    $quantity Quantity in the cart.
 * @return array<string, mixed>|null
 */
function copilot_ac_seed_resolve_item( $sku, $quantity ) {
	$product_id = wc_get_product_id_by_sku( $sku );

	if ( ! $product_id ) {
		return null;
	}

	$product = wc_get_product( $product_id );

	if ( ! $product ) {
		return null;
	}

	$price = (float) wc_get_price_to_display( $product );

	return array(
		'product_id' => (int) $product_id,
		'sku'        => (string) $product->get_sku(),
		'name'       => wp_strip_all_tags( $product->get_name() ),
		'quantity'   => (int) $quantity,
		'price'      => round( $price, 2 ),
		'line_total' => round( $price * (int) $quantity, 2 ),
	);
}

echo $apply ? "MODE: apply\n\n" : "MODE: dry run, nothing will be written\n\n";

$currency = get_woocommerce_currency();
$planned  = array();
$failed   = false;

foreach ( $definitions as $definition ) {
	$items       = array();
	$items_count = 0;
	$cart_total  = 0.0;

	foreach ( $definition['items'] as $wanted ) {
		$item = copilot_ac_seed_resolve_item( $wanted['sku'], $wanted['quantity'] );

		if ( null === $item ) {
			printf( "FAIL: unknown SKU %s in cart %s\n", $wanted['sku'], $definition['slug'] );
			$failed = true;
			continue;
		}

		$items[]      = $item;
		$items_count += $item['quantity'];
		$cart_total  += $item['line_total'];
	}

	if ( empty( $items ) ) {
		continue;
	}

	$updated_stamp = $now_stamp - ( (int) $definition['minutes_ago'] * MINUTE_IN_SECONDS );

	// The cart existed for a while before being abandoned, so created is
	// always earlier than updated. Ten minutes is enough to look real without
	// implying a long shopping session.
	$created_stamp = $updated_stamp - ( 10 * MINUTE_IN_SECONDS );

	$recovered = ! empty( $definition['recovered'] );

	$planned[] = array(
		'session_key'        => $seed_prefix . $definition['slug'],
		'user_id'            => 0,
		'email'              => $definition['email'],
		'first_name'         => $definition['first_name'],
		'currency'           => $currency,
		'items_count'        => $items_count,
		'cart_total'         => round( $cart_total, 2 ),
		'items'              => wp_json_encode( $items ),
		'created_gmt'        => gmdate( 'Y-m-d H:i:s', $created_stamp ),
		'updated_gmt'        => gmdate( 'Y-m-d H:i:s', $updated_stamp ),
		'recovered_order_id' => $recovered ? 999999 : 0,
		'recovered_gmt'      => $recovered ? gmdate( 'Y-m-d H:i:s', $updated_stamp + ( 5 * MINUTE_IN_SECONDS ) ) : null,
		'_minutes_ago'       => (int) $definition['minutes_ago'],
	);
}

if ( $failed ) {
	echo "\nAborted: fix the SKUs above before seeding.\n";
	return;
}

echo "Planned carts:\n";

foreach ( $planned as $row ) {
	printf(
		"  %-14s %-24s %6.2f %s  items=%d  idle=%dm  %s\n",
		$row['session_key'],
		( '' === $row['email'] ? '(no email)' : $row['email'] ),
		$row['cart_total'],
		$row['currency'],
		$row['items_count'],
		$row['_minutes_ago'],
		( $row['recovered_order_id'] > 0 ? 'recovered' : 'abandoned' )
	);
}

if ( ! $apply ) {
	printf( "\nDry run complete: %d carts would be written.\n", count( $planned ) );
	echo "Re-run with the apply argument to write them.\n";
	return;
}

$deleted = $wpdb->query(
	$wpdb->prepare(
		"DELETE FROM {$table} WHERE session_key LIKE %s",
		$wpdb->esc_like( $seed_prefix ) . '%'
	)
);

printf( "\nRemoved %d previously seeded rows.\n", (int) $deleted );

$formats = array( '%s', '%d', '%s', '%s', '%s', '%d', '%f', '%s', '%s', '%s', '%d', '%s' );
$written = 0;

foreach ( $planned as $row ) {
	unset( $row['_minutes_ago'] );

	$result = $wpdb->insert( $table, $row, $formats );

	if ( false === $result ) {
		printf( "FAIL: could not insert %s: %s\n", $row['session_key'], $wpdb->last_error );
		continue;
	}

	++$written;
}

printf( "Inserted %d carts.\n", $written );
echo "OK\n";