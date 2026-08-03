"""
This file only reads environment variables, it contains no secrets itself and is safe to commit.
Put your real values in a ".env" file next to docker-compose.yml (gitignored, see .env.example).

Where to find these values on ManiaServ (maniaservers.com):
Their control panel exposes the dedicated server's XML-RPC host/port and the SuperAdmin/Admin login
used to control it -- switch the server to "PyPlanet" mode in the panel and look for the RPC / controller
connection details there. Never share this password outside your own config.
"""
import os

OWNERS = {
	'default': [
		os.environ.get('PYPLANET_OWNER_LOGIN', 'your-maniaplanet-login'),
	]
}

DEDICATED = {
	'default': {
		'HOST': os.environ.get('PYPLANET_RPC_HOST', '127.0.0.1'),
		'PORT': os.environ.get('PYPLANET_RPC_PORT', '5000'),
		'USER': os.environ.get('PYPLANET_RPC_USER', 'SuperAdmin'),
		'PASSWORD': os.environ.get('PYPLANET_RPC_PASSWORD', 'SuperAdmin'),
	}
}
