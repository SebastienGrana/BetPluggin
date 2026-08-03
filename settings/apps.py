"""
Apps/plugins loaded for this project. The mandatory core apps (maniaplanet, trackmania, statistics, ...) are
always loaded first, you don't need to list them here.
"""

APPS = {
	'default': [
		# Handy defaults, trim or extend as you like.
		'pyplanet.apps.contrib.admin',
		'pyplanet.apps.contrib.info',
		'pyplanet.apps.contrib.players',
		'pyplanet.apps.contrib.funcmd',
		'pyplanet.apps.contrib.clock',
		'pyplanet.apps.contrib.local_records',
		'pyplanet.apps.contrib.mx',
		'pyplanet.apps.contrib.dedimania',

		# "Standard TM2" set added 2026-08-03 (see mapupload/context.txt in
		# ProjetONZSM): jukebox provides /list (map list navigation), the
		# rest is the classic community-server bundle built on top of it.
		'pyplanet.apps.contrib.jukebox',
		'pyplanet.apps.contrib.karma',
		'pyplanet.apps.contrib.rankings',
		'pyplanet.apps.contrib.live_rankings',
		'pyplanet.apps.contrib.best_cps',
		'pyplanet.apps.contrib.sector_times',
		'pyplanet.apps.contrib.voting',
		'pyplanet.apps.contrib.transactions',

		# Our custom betting plugin.
		'apps.betpluggin',
	]
}
