<?php
/**
 * Plugin Name: Copilot Loader
 * Description: Bootstraps Copilot must-use plugins that live in subdirectories.
 * Version: 1.0.0
 *
 * WordPress only auto-loads PHP files placed directly in wp-content/mu-plugins.
 * Files inside subdirectories are ignored, so this loader sits at the root and
 * pulls in the real plugin. Keeping the plugin in its own folder lets it grow
 * into several files without polluting the mu-plugins root.
 *
 * @package CopilotAbandonedCarts
 */

defined( 'ABSPATH' ) || exit;

$copilot_ac_main_file = __DIR__ . '/copilot-abandoned-carts/copilot-abandoned-carts.php';

if ( is_readable( $copilot_ac_main_file ) ) {
	require_once $copilot_ac_main_file;
}

unset( $copilot_ac_main_file );