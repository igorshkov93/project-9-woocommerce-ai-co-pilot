<?php
/**
 * Diagnostics for the Copilot abandoned carts table.
 *
 * Run from the LocalWP site shell:
 *   wp eval-file "C:\path\to\project-9\scripts\inspect_ac_table.php"
 *
 * Read-only: prints the stored schema version, the table structure and the
 * most recent snapshot rows. Never modifies anything.
 *
 * @package CopilotAbandonedCarts
 */

global $wpdb;

if ( ! function_exists( 'copilot_ac_table_name' ) ) {
	echo "FAIL: plugin is not loaded (copilot_ac_table_name is undefined).\n";
	return;
}

$table = copilot_ac_table_name();

echo "Plugin version : " . COPILOT_AC_VERSION . "\n";
echo "Schema version : " . COPILOT_AC_DB_VERSION . " (declared)\n";
echo "Stored option  : " . var_export( get_option( COPILOT_AC_DB_VERSION_OPTION, null ), true ) . "\n";
echo "Table name     : {$table}\n";
echo "Table exists   : " . var_export( copilot_ac_table_exists(), true ) . "\n\n";

if ( ! copilot_ac_table_exists() ) {
	echo "FAIL: table was not created. dbDelta formatting is the usual cause.\n";
	return;
}

echo "Indexes:\n";
$indexes = $wpdb->get_results( "SHOW INDEX FROM {$table}" );
foreach ( $indexes as $index ) {
	printf(
		"  %-20s unique=%-4s seq=%s column=%s\n",
		$index->Key_name,
		( '0' === $index->Non_unique ? 'yes' : 'no' ),
		$index->Seq_in_index,
		$index->Column_name
	);
}

$rows = (int) $wpdb->get_var( "SELECT COUNT(*) FROM {$table}" );
echo "\nRow count      : {$rows}\n";

if ( $rows > 0 ) {
	echo "\nMost recent snapshots:\n";

	$recent = $wpdb->get_results( "SELECT * FROM {$table} ORDER BY updated_gmt DESC LIMIT 5" );

	foreach ( $recent as $row ) {
		echo "  ----------------------------------------\n";
		printf( "  id                 : %s\n", $row->id );
		printf( "  session_key        : %s\n", $row->session_key );
		printf( "  user_id            : %s\n", $row->user_id );
		printf( "  email              : %s\n", ( '' === $row->email ? '(empty)' : $row->email ) );
		printf( "  first_name         : %s\n", ( '' === $row->first_name ? '(empty)' : $row->first_name ) );
		printf( "  currency           : %s\n", $row->currency );
		printf( "  items_count        : %s\n", $row->items_count );
		printf( "  cart_total         : %s\n", $row->cart_total );
		printf( "  created_gmt        : %s\n", $row->created_gmt );
		printf( "  updated_gmt        : %s\n", $row->updated_gmt );
		printf( "  recovered_order_id : %s\n", $row->recovered_order_id );

		$items = json_decode( $row->items, true );

		if ( is_array( $items ) ) {
			echo "  items:\n";
			foreach ( $items as $item ) {
				printf(
					"    %-18s x%-3s %8s  %s\n",
					$item['sku'],
					$item['quantity'],
					$item['line_total'],
					$item['name']
				);
			}
		} else {
			echo "  items: FAIL, invalid JSON\n";
		}
	}
}

echo "\nOK\n";