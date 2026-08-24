<?php
/**
 * REST layer for the Copilot abandoned carts plugin.
 *
 * Exposes a single read-only route consumed by the n8n automation. The
 * contract is documented in docs/adr/0001-abandoned-cart-capture.md and must
 * stay stable: the AI tool layer depends on these exact field names.
 *
 * @package CopilotAbandonedCarts
 */

defined( 'ABSPATH' ) || exit;

/**
 * Registers the abandoned carts route.
 *
 * @return void
 */
function copilot_ac_register_rest_routes() {
	register_rest_route(
		COPILOT_AC_REST_NAMESPACE,
		'/abandoned-carts',
		array(
			'methods'             => WP_REST_Server::READABLE,
			'callback'            => 'copilot_ac_rest_get_abandoned_carts',
			'permission_callback' => 'copilot_ac_rest_permission_check',
			'args'                => copilot_ac_rest_args(),
		)
	);
}

/**
 * Authorization for every route in this namespace.
 *
 * WooCommerce consumer keys do not apply outside the wc/v3 namespace, so
 * authentication happens through WordPress Application Passwords and the
 * resulting user is checked for a store-management capability. Returning true
 * here, or omitting the callback, would publish customer email addresses to
 * anonymous callers.
 *
 * @return true|WP_Error
 */
function copilot_ac_rest_permission_check() {
	if ( current_user_can( 'manage_woocommerce' ) ) {
		return true;
	}

	return new WP_Error(
		'copilot_ac_forbidden',
		__( 'Store management capability is required.', 'copilot-abandoned-carts' ),
		array( 'status' => is_user_logged_in() ? 403 : 401 )
	);
}

/**
 * Declarative argument schema for the route.
 *
 * WordPress validates and sanitizes against this before the callback runs, so
 * the handler never has to defend against out-of-range input.
 *
 * @return array<string, array<string, mixed>>
 */
function copilot_ac_rest_args() {
	return array(
		'threshold_minutes' => array(
			'description'       => 'Minimum idle time, in minutes, before a cart counts as abandoned.',
			'type'              => 'integer',
			'default'           => 60,
			'minimum'           => 5,
			'maximum'           => 10080,
			'sanitize_callback' => 'absint',
			'validate_callback' => 'rest_validate_request_arg',
		),
		'require_email'     => array(
			'description'       => 'Exclude carts with no contact address, which cannot be emailed.',
			'type'              => 'boolean',
			'default'           => true,
			'sanitize_callback' => 'rest_sanitize_boolean',
		),
		'include_recovered' => array(
			'description'       => 'Include carts that already converted into an order. Diagnostics only.',
			'type'              => 'boolean',
			'default'           => false,
			'sanitize_callback' => 'rest_sanitize_boolean',
		),
		'limit'             => array(
			'description'       => 'Maximum number of carts to return.',
			'type'              => 'integer',
			'default'           => 20,
			'minimum'           => 1,
			'maximum'           => 100,
			'sanitize_callback' => 'absint',
			'validate_callback' => 'rest_validate_request_arg',
		),
	);
}

/**
 * Renders a GMT datetime string in the store timezone, ISO 8601 with offset.
 *
 * The conversion is done in PHP rather than with the MySQL CONVERT_TZ
 * function: timezone tables are frequently unpopulated in local MySQL
 * installs, where CONVERT_TZ silently returns NULL. Using wp_timezone() also
 * keeps a single source of truth for what "today" means across this project.
 *
 * @param string $gmt_datetime Datetime in 'Y-m-d H:i:s' GMT.
 * @return string
 */
function copilot_ac_to_local_iso( $gmt_datetime ) {
	if ( empty( $gmt_datetime ) || COPILOT_AC_EPOCH === $gmt_datetime ) {
		return '';
	}

	try {
		$date = new DateTimeImmutable( $gmt_datetime, new DateTimeZone( 'UTC' ) );
	} catch ( Exception $e ) {
		return '';
	}

	return $date->setTimezone( wp_timezone() )->format( DATE_ATOM );
}

/**
 * Renders a GMT datetime string as ISO 8601 in UTC.
 *
 * @param string $gmt_datetime Datetime in 'Y-m-d H:i:s' GMT.
 * @return string
 */
function copilot_ac_to_utc_iso( $gmt_datetime ) {
	if ( empty( $gmt_datetime ) || COPILOT_AC_EPOCH === $gmt_datetime ) {
		return '';
	}

	try {
		$date = new DateTimeImmutable( $gmt_datetime, new DateTimeZone( 'UTC' ) );
	} catch ( Exception $e ) {
		return '';
	}

	return $date->format( 'Y-m-d\TH:i:s\Z' );
}

/**
 * Converts one database row into the public response shape.
 *
 * @param object $row       Raw row from the snapshot table.
 * @param int    $now_stamp Unix timestamp used as "now" for the whole response.
 * @return array<string, mixed>
 */
function copilot_ac_prepare_cart( $row, $now_stamp ) {
	$items = json_decode( (string) $row->items, true );

	if ( ! is_array( $items ) ) {
		$items = array();
	}

	$updated_stamp   = strtotime( $row->updated_gmt . ' UTC' );
	$minutes_elapsed = false === $updated_stamp ? 0 : (int) floor( ( $now_stamp - $updated_stamp ) / MINUTE_IN_SECONDS );

	return array(
		'id'                   => (int) $row->id,
		'email'                => (string) $row->email,
		'first_name'           => (string) $row->first_name,
		'items_count'          => (int) $row->items_count,
		// Currency comes from the row, not from store settings: it was frozen
		// when the snapshot was taken and must not silently change later.
		'cart_total'           => (float) $row->cart_total,
		'currency'             => (string) $row->currency,
		'items'                => $items,
		'created_gmt'          => copilot_ac_to_utc_iso( $row->created_gmt ),
		'updated_gmt'          => copilot_ac_to_utc_iso( $row->updated_gmt ),
		'updated_local'        => copilot_ac_to_local_iso( $row->updated_gmt ),
		'minutes_since_update' => max( 0, $minutes_elapsed ),
		'recovered'            => ( (int) $row->recovered_order_id > 0 ),
		'recovered_order_id'   => (int) $row->recovered_order_id,
	);
}

/**
 * Route handler: returns carts idle for longer than the requested threshold.
 *
 * Abandonment is computed at request time rather than stored as a flag. A cart
 * idle for 40 minutes becomes abandoned an hour later without anything writing
 * to the database, so a stored flag would need a cron job to stay truthful and
 * would be wrong in between runs.
 *
 * @param WP_REST_Request $request Incoming request.
 * @return WP_REST_Response|WP_Error
 */
function copilot_ac_rest_get_abandoned_carts( WP_REST_Request $request ) {
	global $wpdb;

	if ( ! copilot_ac_table_exists() ) {
		// Failing loudly matters here: an empty list would be indistinguishable
		// from "no abandoned carts" and the automation would report good news.
		return new WP_Error(
			'copilot_ac_storage_missing',
			__( 'The abandoned carts table does not exist.', 'copilot-abandoned-carts' ),
			array( 'status' => 500 )
		);
	}

	$threshold         = (int) $request->get_param( 'threshold_minutes' );
	$require_email     = (bool) $request->get_param( 'require_email' );
	$include_recovered = (bool) $request->get_param( 'include_recovered' );
	$limit             = (int) $request->get_param( 'limit' );

	$table     = copilot_ac_table_name();
	$now_stamp = time();
	$cutoff    = gmdate( 'Y-m-d H:i:s', $now_stamp - ( $threshold * MINUTE_IN_SECONDS ) );

	$where  = array( 'updated_gmt <= %s' );
	$params = array( $cutoff );

	if ( ! $include_recovered ) {
		$where[] = 'recovered_order_id = 0';
	}

	if ( $require_email ) {
		$where[] = "email <> ''";
	}

	$sql = "SELECT * FROM {$table} WHERE " . implode( ' AND ', $where ) . ' ORDER BY updated_gmt DESC LIMIT %d';

	$params[] = $limit;

	$rows = $wpdb->get_results( $wpdb->prepare( $sql, $params ) );

	$carts = array();

	foreach ( (array) $rows as $row ) {
		$carts[] = copilot_ac_prepare_cart( $row, $now_stamp );
	}

	$response = array(
		'generated_at_gmt'   => gmdate( 'Y-m-d\TH:i:s\Z', $now_stamp ),
		'generated_at_local' => wp_date( DATE_ATOM, $now_stamp ),
		'timezone'           => wp_timezone_string(),
		'threshold_minutes'  => $threshold,
		'store_currency'     => get_woocommerce_currency(),
		'count'              => count( $carts ),
		'carts'              => $carts,
	);

	return new WP_REST_Response( $response, 200 );
}