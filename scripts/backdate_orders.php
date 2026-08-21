<?php
/**
 * Spread seeded orders across the last sixty days.
 *
 * The WooCommerce REST API refuses to set an order creation date: the field is
 * readonly in the orders controller and is discarded silently, without an
 * error. Since HPOS is enabled and order-to-post synchronisation is disabled,
 * the authoritative value lives in a single column of wp_wc_orders.
 *
 * This is a seeding-time workaround only. Runtime access stays purely through
 * the REST API.
 *
 * Run from the LocalWP site shell:
 *   wp eval-file <path>\backdate_orders.php
 *   wp eval-file <path>\backdate_orders.php apply
 *
 * Without the "apply" argument the script only reports what it would change.
 */

global $wpdb;

$apply    = in_array( 'apply', $args, true );
$table    = $wpdb->prefix . 'wc_orders';
$days     = 60;
$seed     = 20260821;
$timezone = new DateTimeZone( 'Europe/Kyiv' );
$utc      = new DateTimeZone( 'UTC' );

// Deterministic: the same seed reproduces the same layout on a rebuilt stand.
mt_srand( $seed );

$order_ids = $wpdb->get_col( "SELECT id FROM {$table} ORDER BY id ASC" );
$total     = count( $order_ids );

if ( 0 === $total ) {
	WP_CLI::error( 'No orders found.' );
}

// Build the per-day volume curve first, then stretch it to match the real
// number of orders so nothing is left over.
$today   = new DateTime( 'now', $timezone );
$today->setTime( 12, 0, 0 );
$weights = array();

for ( $days_ago = $days - 1; $days_ago >= 0; $days_ago-- ) {
	$day     = ( clone $today )->modify( "-{$days_ago} days" );
	$weekend = (int) $day->format( 'N' ) >= 6;
	$base    = $weekend ? mt_rand( 3, 5 ) : mt_rand( 2, 4 );
	$growth  = 1.0 + 0.4 * ( 1.0 - $days_ago / max( $days - 1, 1 ) );

	$weights[] = array(
		'days_ago' => $days_ago,
		'weight'   => max( 1.0, $base * $growth ),
	);
}

$weight_sum = array_sum( array_column( $weights, 'weight' ) );

$plan      = array();
$assigned  = 0;
foreach ( $weights as $index => $entry ) {
	$share = (int) round( $total * $entry['weight'] / $weight_sum );
	if ( $index === count( $weights ) - 1 ) {
		$share = $total - $assigned; // Absorb rounding drift on the last day.
	}
	$share = max( 0, min( $share, $total - $assigned ) );

	$plan[] = array(
		'days_ago' => $entry['days_ago'],
		'count'    => $share,
	);
	$assigned += $share;
}

$updated   = 0;
$cursor    = 0;
$night     = 0;
$by_day    = array();

foreach ( $plan as $entry ) {
	for ( $i = 0; $i < $entry['count']; $i++ ) {
		if ( ! isset( $order_ids[ $cursor ] ) ) {
			break 2;
		}

		$order_id = (int) $order_ids[ $cursor ];
		$cursor++;

		$local = ( clone $today )->modify( "-{$entry['days_ago']} days" );

		// Roughly every twelfth order lands between midnight and 03:00 local
		// time. In UTC those belong to the previous calendar day, which is what
		// the daily summary tool must handle correctly.
		$is_night = ( 0 === $cursor % 12 );
		$hour     = $is_night ? mt_rand( 0, 2 ) : mt_rand( 9, 22 );
		$local->setTime( $hour, mt_rand( 0, 59 ), mt_rand( 0, 59 ) );

		if ( $is_night ) {
			$night++;
		}

		$gmt = ( clone $local )->setTimezone( $utc );
		$stamp = $gmt->format( 'Y-m-d H:i:s' );

		$by_day[ $local->format( 'Y-m-d' ) ] = ( $by_day[ $local->format( 'Y-m-d' ) ] ?? 0 ) + 1;

		if ( $apply ) {
			$wpdb->update(
				$table,
				array(
					'date_created_gmt' => $stamp,
					'date_updated_gmt' => $stamp,
				),
				array( 'id' => $order_id ),
				array( '%s', '%s' ),
				array( '%d' )
			);
		}

		$updated++;
	}
}

if ( $apply ) {
	// Woo caches order objects. Without flushing, the REST API keeps serving
	// the old dates and the change looks like it never happened.
	foreach ( $order_ids as $order_id ) {
		wp_cache_delete( (int) $order_id, 'orders' );
	}
	wp_cache_flush();

	$wpdb->query(
		"DELETE FROM {$wpdb->options} WHERE option_name LIKE '_transient_wc_report%' OR option_name LIKE '_transient_timeout_wc_report%'"
	);
}

ksort( $by_day );
$days_used = count( $by_day );
$first_day = array_key_first( $by_day );
$last_day  = array_key_last( $by_day );

WP_CLI::log( $apply ? 'mode: APPLY' : 'mode: DRY-RUN' );
WP_CLI::log( "orders processed:  {$updated}" );
WP_CLI::log( "distinct days:     {$days_used}" );
WP_CLI::log( "date range:        {$first_day} .. {$last_day}" );
WP_CLI::log( "night orders:      {$night}" );
WP_CLI::log( '' );
WP_CLI::log( 'orders per day (local time, last 7 days):' );

$tail = array_slice( $by_day, -7, 7, true );
foreach ( $tail as $day => $count ) {
	WP_CLI::log( sprintf( '  %s  %d', $day, $count ) );
}

if ( ! $apply ) {
	WP_CLI::log( '' );
	WP_CLI::log( 'Nothing was written. Re-run with the "apply" argument to commit.' );
}