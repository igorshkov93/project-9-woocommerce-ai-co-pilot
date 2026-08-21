<?php
/**
 * Guarantee that today contains an order placed between 00:00 and 03:00 local
 * time.
 *
 * In UTC such an order belongs to yesterday. It is the regression test for the
 * daily summary tool: a naive implementation that ignores the business
 * timezone will return a different count and the verification script will say
 * so.
 *
 * The backdating pass distributes night orders by position, which does not
 * guarantee coverage of the current day, so this runs afterwards.
 *
 * Run from the LocalWP site shell:
 *   wp eval-file <path>\add_night_order.php
 *   wp eval-file <path>\add_night_order.php apply
 */

global $wpdb;

$apply      = in_array( 'apply', $args, true );
$orders_tbl = $wpdb->prefix . 'wc_orders';
$timezone   = new DateTimeZone( 'Europe/Kyiv' );
$utc        = new DateTimeZone( 'UTC' );

$today_local = new DateTime( 'now', $timezone );
$today_key   = $today_local->format( 'Y-m-d' );

// Target: 01:30 local time today.
$target_local = new DateTime( $today_key . ' 01:30:00', $timezone );
$target_gmt   = ( clone $target_local )->setTimezone( $utc );

WP_CLI::log( 'local target: ' . $target_local->format( 'Y-m-d H:i:s T' ) );
WP_CLI::log( 'stored as:    ' . $target_gmt->format( 'Y-m-d H:i:s' ) . ' UTC' );
WP_CLI::log( 'utc date:     ' . $target_gmt->format( 'Y-m-d' ) . ' <- differs from local date when it works' );
WP_CLI::log( '' );

if ( $target_gmt->format( 'Y-m-d' ) === $today_key ) {
	WP_CLI::warning( 'No date shift at this hour. The offset may be zero.' );
}

// Pick the most recent order from today and move it into the night window.
$candidate = $wpdb->get_row(
	$wpdb->prepare(
		"SELECT id, date_created_gmt, status FROM {$orders_tbl}
		 WHERE type = 'shop_order' AND date_created_gmt >= %s
		 ORDER BY date_created_gmt DESC LIMIT 1",
		$today_key . ' 00:00:00'
	)
);

if ( ! $candidate ) {
	WP_CLI::error( 'No order found for today.' );
}

WP_CLI::log( sprintf( 'moving order %d (%s, %s)', $candidate->id, $candidate->status, $candidate->date_created_gmt ) );

if ( $apply ) {
	$stamp = $target_gmt->format( 'Y-m-d H:i:s' );
	$wpdb->update(
		$orders_tbl,
		array(
			'date_created_gmt' => $stamp,
			'date_updated_gmt' => $stamp,
		),
		array( 'id' => (int) $candidate->id ),
		array( '%s', '%s' ),
		array( '%d' )
	);
	wp_cache_delete( (int) $candidate->id, 'orders' );
	wp_cache_flush();
	WP_CLI::log( '' );
	WP_CLI::log( 'mode: APPLY -- order moved' );
} else {
	WP_CLI::log( '' );
	WP_CLI::log( 'mode: DRY-RUN' );
	WP_CLI::log( 'Nothing was written. Re-run with the "apply" argument to commit.' );
}