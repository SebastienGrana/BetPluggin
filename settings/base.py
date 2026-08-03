"""
Non-secret defaults for this PyPlanet project. Safe to commit.
Server credentials and your ManiaPlanet login live in settings/local.py, which reads them from
environment variables so nothing sensitive ends up in git.
"""
import os

ROOT_PATH = os.path.dirname(os.path.dirname(__file__))
TMP_PATH = os.path.join(ROOT_PATH, 'tmp')

if not os.path.exists(TMP_PATH):
	os.mkdir(TMP_PATH)

DEBUG = bool(os.environ.get('PYPLANET_DEBUG', False))

POOLS = [
	'default'
]

# peewee_async (pinned by PyPlanet) only supports MySQL/PostgreSQL, not SQLite -- see the "db" service
# in docker-compose.yml. Internal-only credentials, never exposed outside the docker network.
DATABASES = {
	'default': {
		'ENGINE': 'peewee_async.MySQLDatabase',
		'NAME': 'pyplanet',
		'OPTIONS': {
			'host': os.environ.get('PYPLANET_DB_HOST', 'db'),
			'user': os.environ.get('PYPLANET_DB_USER', 'pyplanet'),
			'password': os.environ.get('PYPLANET_DB_PASSWORD', 'pyplanet'),
			'charset': 'utf8mb4',
		}
	}
}

STORAGE = {
	'default': {
		'DRIVER': 'pyplanet.core.storage.drivers.local.LocalDriver',
		'OPTIONS': {},
	}
}

MAP_MATCHSETTINGS = {
	'default': 'maplist.txt',
}

BLACKLIST_FILE = {
	'default': 'blacklist.txt'
}

SONGS = {
	'default': []
}

SELF_UPGRADE = False
ALLOW_SLOTS_CHANGE = True
