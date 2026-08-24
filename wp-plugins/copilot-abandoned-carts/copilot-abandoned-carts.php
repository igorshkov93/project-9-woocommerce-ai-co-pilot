<?php
/**
 * Plugin Name: Copilot Abandoned Carts
 * Description: Captures WooCommerce cart snapshots and exposes abandoned carts over the REST API for the AI co-pilot.
 * Version: 0.5.0
 * Author: Igor Shkov
 * Requires PHP: 7.4
 *
 * This is a must-use plugin. It cannot be deactivated from the admin UI, which
 * is deliberate: a live demo must not break because a checkbox was toggled.
 * See docs/adr/0001-abandoned-cart-capture.md for the design rationale.
 *
 * @package CopilotAbandonedCarts
 */

defined( 'ABSPATH' ) || exit;

// The loader uses require_once, but a stray manual include must not fatal.
if ( defined( 'COPILOT_AC_VERSION' ) ) {
	return;
}

define( 'COPILOT_AC_VERSION', '0.5.0' );

// Bumped whenever the table schema changes, so plugins_loaded can migrate.
// v2 replaced UNIQUE(session_key) with UNIQUE(session_key, recovered_order_id)
// so that a session can hold one active cart plus any number of recovered ones.
define( 'COPILOT_AC_DB_VERSION', '2' );
define( 'COPILOT_AC_DB_VERSION_OPTION', 'copilot_abandoned_carts_db_version' );

define( 'COPILOT_AC_TABLE_SUFFIX', 'copilot_abandoned_carts' );
define( 'COPILOT_AC_REST_NAMESPACE', 'copilot/v1' );
define( 'COPILOT_AC_MAIN_FILE', __FILE__ );

/**
 * Order meta key holding the snapshot id an order was recovered from.
 *
 * Under HPOS this must be written through WC_Order::update_meta_data(), never
 * update_post_meta(): orders do not live in wp_posts at all.
 */
define( 'COPILOT_AC_ORDER_META_KEY', '_copilot_abandoned_cart_id' );

/**
 * Sentinel used instead of the MySQL zero date.
 *
 * MySQL 8 rejects '0000-00-00 00:00:00' under the default sql_mode. WordPress
 * strips NO_ZERO_DATE from the session, but relying on that is unnecessary:
 * an explicit epoch default is valid under every sql_mode and still sorts
 * before any real timestamp.
 */
define( 'COPILOT_AC_EPOCH', '1970-01-01 00:00:00' );

require_once __DIR__ . '/rest-controller.php';

/**
 * Request-scoped state.
 *
 * Cart hooks fire several times per request. Writing on each of them would
 * mean three to five queries per click and, worse, would snapshot totals that
 * WooCommerce has not finished recalculating. Instead the hooks only raise a
 * flag and the single write happens on shutdown.
 *
 * @var array{dirty: bool, flushed: bool}
 */
$GLOBALS['copilot_ac_state'] = array(
	'dirty'   => false,
	'flushed' => false,
);

/* -------------------------------------------------------------------------
 * Schema
 * ---------------------------------------------------------------------- */

/**
 * Full name of the snapshot table, including the site table prefix.
 *
 * @return string
 */
function copilot_ac_table_name() {
	global $wpdb;

	return $wpdb->prefix . COPILOT_AC_TABLE_SUFFIX;
}

/**
 * Whether WooCommerce is loaded and usable.
 *
 * Must-use plugins load before regular plugins, so this can only be called
 * from plugins_loaded or later. Calling it at file scope always returns false.
 *
 * @return bool
 */
function copilot_ac_is_woocommerce_active() {
	return class_exists( 'WooCommerce' );
}

/**
 * Whether the snapshot table currently exists in the database.
 *
 * Used by diagnostics and by the REST layer, which must fail loudly rather
 * than return an empty list when storage is missing.
 *
 * @return bool
 */
function copilot_ac_table_exists() {
	global $wpdb;

	$table = copilot_ac_table_name();

	$found = $wpdb->get_var( $wpdb->prepare( 'SHOW TABLES LIKE %s', $table ) );

	return $found === $table;
}

/**
 * The CREATE TABLE statement fed to dbDelta().
 *
 * dbDelta parses this string with regular expressions rather than a real SQL
 * parser, so the formatting is load-bearing:
 *   - one column per line;
 *   - two spaces between PRIMARY KEY and the column list;
 *   - every index explicitly named.
 * Deviating from this makes dbDelta silently create nothing.
 *
 * @return string
 */
function copilot_ac_schema_sql() {
	global $wpdb;

	$table           = copilot_ac_table_name();
	$charset_collate = $wpdb->get_charset_collate();
	$epoch           = COPILOT_AC_EPOCH;

	return "CREATE TABLE {$table} (
	id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
	session_key VARCHAR(64) NOT NULL DEFAULT '',
	user_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
	email VARCHAR(190) NOT NULL DEFAULT '',
	first_name VARCHAR(100) NOT NULL DEFAULT '',
	currency CHAR(3) NOT NULL DEFAULT '',
	items_count SMALLINT UNSIGNED NOT NULL DEFAULT 0,
	cart_total DECIMAL(12,2) NOT NULL DEFAULT 0.00,
	items LONGTEXT NOT NULL,
	created_gmt DATETIME NOT NULL DEFAULT '{$epoch}',
	updated_gmt DATETIME NOT NULL DEFAULT '{$epoch}',
	recovered_order_id BIGINT UNSIGNED NOT NULL DEFAULT 0,
	recovered_gmt DATETIME NULL DEFAULT NULL,
	PRIMARY KEY  (id),
	UNIQUE KEY session_recovery (session_key, recovered_order_id),
	KEY session_lookup (session_key),
	KEY updated_gmt (updated_gmt),
	KEY recovered_order_id (recovered_order_id)
) {$charset_collate};";
}

/**
 * Drops schema-v1 indexes that dbDelta cannot remove on its own.
 *
 * dbDelta only ever adds columns and indexes. Replacing the v1 unique index on
 * session_key with the v2 composite one therefore needs an explicit ALTER,
 * guarded so that a fresh install does not try to drop a nonexistent index.
 *
 * @return void
 */
function copilot_ac_drop_legacy_indexes() {
	global $wpdb;

	if ( ! copilot_ac_table_exists() ) {
		return;
	}

	$table = copilot_ac_table_name();

	$legacy = $wpdb->get_results( "SHOW INDEX FROM {$table} WHERE Key_name = 'session_key'" );

	if ( ! empty( $legacy ) ) {
		$wpdb->query( "ALTER TABLE {$table} DROP INDEX session_key" );
	}
}

/**
 * Creates or migrates the snapshot table, then records the schema version.
 *
 * dbDelta() is idempotent: it diffs the declared schema against the live table
 * and issues only the missing ALTERs. It is still guarded by a version option
 * so the diff does not run on every single page load.
 *
 * @param bool $force Run regardless of the stored version. Used by diagnostics.
 * @return bool True when dbDelta was executed.
 */
function copilot_ac_maybe_install_schema( $force = false ) {
	$installed = get_option( COPILOT_AC_DB_VERSION_OPTION, '' );

	if ( ! $force && (string) $installed === (string) COPILOT_AC_DB_VERSION ) {
		return false;
	}

	require_once ABSPATH . 'wp-admin/includes/upgrade.php';

	copilot_ac_drop_legacy_indexes();

	dbDelta( copilot_ac_schema_sql() );

	// autoload = yes: this option is read on every request, so it must not
	// cost an extra query.
	update_option( COPILOT_AC_DB_VERSION_OPTION, COPILOT_AC_DB_VERSION, true );

	return true;
}

/* -------------------------------------------------------------------------
 * Capture: deciding whether to run at all
 * ---------------------------------------------------------------------- */

/**
 * Whether a cart snapshot can be taken in the current request.
 *
 * WooCommerce only instantiates the cart and session on front-end and Store
 * API requests. Under WP-CLI a session may technically exist but capturing
 * there would let maintenance scripts pollute real shopper data, so CLI is
 * excluded outright; the seeding script writes rows directly instead.
 *
 * @return bool
 */
function copilot_ac_can_capture() {
	if ( defined( 'WP_CLI' ) && WP_CLI ) {
		return false;
	}

	if ( ! function_exists( 'WC' ) ) {
		return false;
	}

	$wc = WC();

	if ( ! $wc || ! isset( $wc->cart ) || ! is_a( $wc->cart, 'WC_Cart' ) ) {
		return false;
	}

	if ( ! isset( $wc->session ) || ! is_a( $wc->session, 'WC_Session' ) ) {
		return false;
	}

	return '' !== (string) $wc->session->get_customer_id();
}

/**
 * Marks the request as needing a snapshot write.
 *
 * Every cart hook funnels through here. The actual database work is deferred
 * to shutdown so that repeated hook firings collapse into one write.
 *
 * @return void
 */
function copilot_ac_mark_dirty() {
	$GLOBALS['copilot_ac_state']['dirty'] = true;
}

/* -------------------------------------------------------------------------
 * Capture: building the snapshot
 * ---------------------------------------------------------------------- */

/**
 * Serializes the current cart contents into the documented item shape.
 *
 * Unit prices are frozen here on purpose. A recovery email sent days later
 * must quote the amount the shopper actually saw, not a price that changed in
 * the meantime.
 *
 * @return array<int, array<string, mixed>>
 */
function copilot_ac_build_items() {
	$items = array();

	foreach ( WC()->cart->get_cart() as $cart_item ) {
		if ( empty( $cart_item['data'] ) || ! is_a( $cart_item['data'], 'WC_Product' ) ) {
			continue;
		}

		/**
		 * Product instance for this cart line.
		 *
		 * @var WC_Product $product
		 */
		$product  = $cart_item['data'];
		$quantity = (int) $cart_item['quantity'];

		if ( $quantity < 1 ) {
			continue;
		}

		$price = (float) wc_get_price_to_display( $product );

		$items[] = array(
			'product_id' => (int) $product->get_id(),
			'sku'        => (string) $product->get_sku(),
			'name'       => wp_strip_all_tags( $product->get_name() ),
			'quantity'   => $quantity,
			'price'      => round( $price, 2 ),
			'line_total' => round( $price * $quantity, 2 ),
		);
	}

	return $items;
}

/**
 * Reads whatever contact details WooCommerce currently holds for the shopper.
 *
 * For logged-in customers this is populated from the account. For guests it
 * only becomes available once checkout fields have been filled in, which is
 * why the checkout hooks below push data into the customer object first.
 *
 * @return array{email: string, first_name: string}
 */
function copilot_ac_read_contact() {
	$email      = '';
	$first_name = '';

	$customer = WC()->customer;

	if ( is_a( $customer, 'WC_Customer' ) ) {
		$email      = (string) $customer->get_billing_email();
		$first_name = (string) $customer->get_billing_first_name();

		if ( '' === $email ) {
			$email = (string) $customer->get_email();
		}

		if ( '' === $first_name ) {
			$first_name = (string) $customer->get_first_name();
		}
	}

	if ( '' === $email && is_user_logged_in() ) {
		$user  = wp_get_current_user();
		$email = (string) $user->user_email;
	}

	$email = is_email( $email ) ? sanitize_email( $email ) : '';

	return array(
		'email'      => $email,
		'first_name' => sanitize_text_field( $first_name ),
	);
}

/* -------------------------------------------------------------------------
 * Capture: persistence
 * ---------------------------------------------------------------------- */

/**
 * Fetches the active (not yet recovered) snapshot row for a session.
 *
 * @param string $session_key WooCommerce session identifier.
 * @return object|null
 */
function copilot_ac_get_active_row( $session_key ) {
	global $wpdb;

	$table = copilot_ac_table_name();

	return $wpdb->get_row(
		$wpdb->prepare(
			"SELECT * FROM {$table} WHERE session_key = %s AND recovered_order_id = 0 LIMIT 1",
			$session_key
		)
	);
}

/**
 * Writes the current cart state to the database.
 *
 * Runs once per request, on shutdown. An empty cart deletes the active row:
 * a shopper who removed everything did not abandon a cart, and keeping the
 * row would produce emails about products nobody wants.
 *
 * A cart emptied by a completed checkout does not reach the delete branch,
 * because the recovery hook has already set recovered_order_id on that row
 * and it is therefore no longer the active one.
 *
 * @return void
 */
function copilot_ac_flush() {
	global $wpdb;

	$state = &$GLOBALS['copilot_ac_state'];

	if ( ! $state['dirty'] || $state['flushed'] ) {
		return;
	}

	$state['flushed'] = true;

	if ( ! copilot_ac_can_capture() || ! copilot_ac_table_exists() ) {
		return;
	}

	$table       = copilot_ac_table_name();
	$session_key = (string) WC()->session->get_customer_id();
	$existing    = copilot_ac_get_active_row( $session_key );
	$items       = copilot_ac_build_items();

	if ( empty( $items ) ) {
		if ( $existing ) {
			$wpdb->delete( $table, array( 'id' => (int) $existing->id ), array( '%d' ) );
		}

		return;
	}

	$contact     = copilot_ac_read_contact();
	$now_gmt     = current_time( 'mysql', true );
	$items_count = 0;
	$cart_total  = 0.0;

	foreach ( $items as $item ) {
		$items_count += (int) $item['quantity'];
		$cart_total  += (float) $item['line_total'];
	}

	// The total is summed from the frozen line totals rather than read from
	// WC_Cart, so that cart_total and items can never disagree in the email.
	$data = array(
		'session_key' => $session_key,
		'user_id'     => (int) get_current_user_id(),
		'email'       => $contact['email'],
		'first_name'  => $contact['first_name'],
		'currency'    => get_woocommerce_currency(),
		'items_count' => $items_count,
		'cart_total'  => round( $cart_total, 2 ),
		'items'       => wp_json_encode( $items ),
		'updated_gmt' => $now_gmt,
	);

	$formats = array( '%s', '%d', '%s', '%s', '%s', '%d', '%f', '%s', '%s' );

	if ( $existing ) {
		// An address captured earlier must not be wiped by a later cart change
		// that happens before checkout fields are filled in again.
		if ( '' === $data['email'] && '' !== (string) $existing->email ) {
			$data['email'] = (string) $existing->email;
		}

		if ( '' === $data['first_name'] && '' !== (string) $existing->first_name ) {
			$data['first_name'] = (string) $existing->first_name;
		}

		$wpdb->update( $table, $data, array( 'id' => (int) $existing->id ), $formats, array( '%d' ) );

		return;
	}

	$data['created_gmt']        = $now_gmt;
	$data['recovered_order_id'] = 0;

	$formats[] = '%s';
	$formats[] = '%d';

	$wpdb->insert( $table, $data, $formats );
}

/* -------------------------------------------------------------------------
 * Capture: contact details from the two checkout implementations
 * ---------------------------------------------------------------------- */

/**
 * Classic checkout: pulls the email out of the AJAX order-review payload.
 *
 * The shortcode checkout posts the whole form as a URL-encoded string on every
 * field change. WooCommerce does not write those values to the customer object
 * until the order is placed, so they are copied across manually here.
 *
 * @param string $post_data URL-encoded checkout form data.
 * @return void
 */
function copilot_ac_capture_classic_checkout_contact( $post_data ) {
	if ( ! is_string( $post_data ) || '' === $post_data ) {
		return;
	}

	$fields = array();
	parse_str( $post_data, $fields );

	if ( ! empty( $fields['billing_email'] ) && is_email( $fields['billing_email'] ) ) {
		WC()->customer->set_billing_email( sanitize_email( $fields['billing_email'] ) );
	}

	if ( ! empty( $fields['billing_first_name'] ) ) {
		WC()->customer->set_billing_first_name( sanitize_text_field( $fields['billing_first_name'] ) );
	}

	copilot_ac_mark_dirty();
}

/**
 * Block checkout: the Store API has already updated the customer object.
 *
 * Nothing needs copying here, the snapshot just has to be refreshed so the
 * address lands in our table.
 *
 * @return void
 */
function copilot_ac_capture_store_api_contact() {
	copilot_ac_mark_dirty();
}

/* -------------------------------------------------------------------------
 * Recovery
 * ---------------------------------------------------------------------- */

/**
 * Marks the session's active snapshot as recovered by a real order.
 *
 * Ordering matters. WooCommerce creates the order, then empties the cart,
 * which fires woocommerce_cart_emptied and would otherwise make the shutdown
 * flush delete the row. Because this runs first and moves the row out of the
 * "active" set, the flush finds nothing to delete. The composite unique index
 * on (session_key, recovered_order_id) allows the same session to accumulate
 * several recovered rows plus one new active cart afterwards.
 *
 * @param int|WC_Order $order Order id or object, depending on the hook.
 * @return void
 */
function copilot_ac_mark_recovered( $order ) {
	global $wpdb;

	if ( ! copilot_ac_table_exists() ) {
		return;
	}

	$order = is_a( $order, 'WC_Order' ) ? $order : wc_get_order( $order );

	if ( ! is_a( $order, 'WC_Order' ) ) {
		return;
	}

	$order_id = (int) $order->get_id();

	if ( $order_id <= 0 ) {
		return;
	}

	if ( ! function_exists( 'WC' ) || ! isset( WC()->session ) || ! is_a( WC()->session, 'WC_Session' ) ) {
		return;
	}

	$session_key = (string) WC()->session->get_customer_id();

	if ( '' === $session_key ) {
		return;
	}

	$row = copilot_ac_get_active_row( $session_key );

	if ( ! $row ) {
		// No snapshot for this session: the shopper may have bought through a
		// path that never touched the cart. Nothing to reconcile.
		return;
	}

	$table   = copilot_ac_table_name();
	$now_gmt = current_time( 'mysql', true );

	$data    = array(
		'recovered_order_id' => $order_id,
		'recovered_gmt'      => $now_gmt,
	);
	$formats = array( '%d', '%s' );

	// A guest who typed the address only in the final submit never triggered
	// the AJAX capture, so the order is the more reliable source here.
	if ( '' === (string) $row->email ) {
		$order_email = (string) $order->get_billing_email();

		if ( is_email( $order_email ) ) {
			$data['email'] = sanitize_email( $order_email );
			$formats[]     = '%s';
		}
	}

	if ( '' === (string) $row->first_name ) {
		$order_first_name = (string) $order->get_billing_first_name();

		if ( '' !== $order_first_name ) {
			$data['first_name'] = sanitize_text_field( $order_first_name );
			$formats[]          = '%s';
		}
	}

	$updated = $wpdb->update( $table, $data, array( 'id' => (int) $row->id ), $formats, array( '%d' ) );

	if ( false === $updated ) {
		return;
	}

	// Back-reference for auditing and for the architecture write-up. Under
	// HPOS orders are not posts, so this must go through the order object.
	$order->update_meta_data( COPILOT_AC_ORDER_META_KEY, (int) $row->id );
	$order->save();
}

/**
 * Store API checkout wrapper.
 *
 * The block checkout passes the order object; the signature is kept separate
 * so the intent of each hook stays readable.
 *
 * @param WC_Order $order Created order.
 * @return void
 */
function copilot_ac_mark_recovered_store_api( $order ) {
	copilot_ac_mark_recovered( $order );
}

/* -------------------------------------------------------------------------
 * Bootstrap
 * ---------------------------------------------------------------------- */

/**
 * Registers every cart hook that should trigger a snapshot.
 *
 * @return void
 */
function copilot_ac_register_capture_hooks() {
	$cart_hooks = array(
		'woocommerce_add_to_cart',
		'woocommerce_cart_item_removed',
		'woocommerce_cart_item_restored',
		'woocommerce_after_cart_item_quantity_update',
		'woocommerce_cart_emptied',
		'woocommerce_applied_coupon',
		'woocommerce_removed_coupon',
	);

	foreach ( $cart_hooks as $hook ) {
		add_action( $hook, 'copilot_ac_mark_dirty', 100 );
	}

	add_action( 'woocommerce_checkout_update_order_review', 'copilot_ac_capture_classic_checkout_contact', 100 );
	add_action( 'woocommerce_store_api_cart_update_customer_from_request', 'copilot_ac_capture_store_api_contact', 100 );

	// Single write per request, after WooCommerce has finished recalculating.
	add_action( 'shutdown', 'copilot_ac_flush', 5 );
}

/**
 * Registers the hooks that close the loop when an order is created.
 *
 * Both checkout implementations are covered: the shortcode checkout and the
 * Store API used by the block checkout. Missing either one would leave a class
 * of converted carts looking abandoned forever.
 *
 * @return void
 */
function copilot_ac_register_recovery_hooks() {
	add_action( 'woocommerce_checkout_order_processed', 'copilot_ac_mark_recovered', 10, 1 );
	add_action( 'woocommerce_store_api_checkout_order_processed', 'copilot_ac_mark_recovered_store_api', 10, 1 );
}

/**
 * Entry point. Everything the plugin does hangs off this function.
 *
 * Priority 20 guarantees WooCommerce has finished loading. Without a late
 * priority the WooCommerce check below would silently disable the plugin.
 */
function copilot_ac_bootstrap() {
	if ( ! copilot_ac_is_woocommerce_active() ) {
		return;
	}

	copilot_ac_maybe_install_schema();
	copilot_ac_register_capture_hooks();
	copilot_ac_register_recovery_hooks();

	add_action( 'rest_api_init', 'copilot_ac_register_rest_routes' );
}
add_action( 'plugins_loaded', 'copilot_ac_bootstrap', 20 );