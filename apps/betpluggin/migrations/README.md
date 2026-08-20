# Migrations

**Any change to a field in `../models.py` must ship a migration file in this folder, in the same
commit.** Without one, a fresh database is fine and production crashes on a missing column.

## Why the two behave differently

On startup PyPlanet creates the tables of any app whose tables don't exist yet, and flags that app as
"initial setup" (`pyplanet/core/db/migrator.py`). For a flagged app, every pending migration file is
recorded as applied *without being run* — correct, because the table was just built from the current
`models.py` and already has the new column.

An app whose table already exists is not flagged, so its pending migrations are executed for real.

That is the whole trap: on your machine a change to `models.py` appears to work because the database
gets rebuilt from scratch. Production has had the `bet` table since its first boot and will never be
rebuilt, so the column only ever appears if a migration adds it.

State as of 2026-08-16: the `migration` table holds **no** `betpluggin` rows at all (the app was set
up before this folder had any content, and an empty folder records nothing). So the first file added
here runs for real on every server that already has a `bet` table.

## Never add a new model and a column migration in the same release

`create_tables()` flags an app when **any** of its models is missing a table:

```python
for name, (app, name, model) in self.db.registry.models.items():
    if not model.table_exists():
        creating.append(model)
        self.pass_migrations.add(app.label)   # the whole app, not just this model
```

The flag is per *app*, not per model, and it is set before any migration is looked at. So adding a
second model to `models.py` in the same release as a migration that alters `bet` gives you, on
production: the new table created, the new columns on `bet` **missing**, and the migration recorded
as applied. It will never run again. Every insert touching the new columns then fails, and a fresh
local database still passes because there the columns come from `models.py` directly.

Split it across two deploys, in this order:

1. The migration that alters existing tables, with **no** new model. The app is not flagged, so it
   runs for real. Confirm with `DESCRIBE bet;` before continuing.
2. The new model. The app is flagged this time, but nothing is left unapplied to fake.

There is no single-deploy workaround: the flag is set in `create_tables()`, which runs before the
migration loop, so a migration cannot outrun it.

## Writing one

Name it `NNN_short_description.py`, zero-padded — files are sorted as text, so `10_x` sorts before
`9_x`. Use **exactly one dot** in the filename: the loader does `name, ext = file_name.split('.')`
and a second dot raises.

**Never put an `__init__.py` here.** Migrations are discovered with `glob('*.py')`, so it would be
picked up as a migration named `__init__` and the loader would call `upgrade()` on it. The folder
works as a namespace package without one.

```python
from peewee import *
from playhouse.migrate import migrate, SchemaMigrator

from ..models import Bet


def upgrade(migrator: SchemaMigrator):
	# Every added column must be null=True or carry a default: the rows already in the table need a
	# value for it.
	settled_at = DateTimeField(null=True)

	migrate(
		migrator.add_column(Bet._meta.db_table, 'settled_at', settled_at),
	)


def downgrade(migrator: SchemaMigrator):
	migrate(
		migrator.drop_column(Bet._meta.db_table, 'settled_at'),
	)
```

`Bet._meta.db_table` is `bet`, not `betpluggin_bet` — don't hardcode the name, read it off the model.

Renaming or retyping a column uses `migrator.rename_column` / `migrator.drop_not_null` and friends
from `playhouse.migrate`; see `pyplanet/apps/core/maniaplanet/migrations/` for worked examples.

## Testing one before it ships

A migration that only ever runs against a fresh database has not been tested — that path fakes it.
To exercise the real path, apply it to a database that already has the old schema:

```bash
docker compose exec db mysql -upyplanet -ppyplanet pyplanet -e "DESCRIBE bet;"
```

Check the column is absent, restart the plugin, then run the same command again and confirm it
appeared. `docker compose logs betpluggin | grep -i migration` shows either
`Successfully executed migration: ...` or the `Can't migrate ...` warning that precedes the raise.

Dropping the whole database to "start clean" defeats the point: it puts you back on the faked path,
which is the one that always passes.

## RaceResult (deployment 2)

`RaceResult` is a brand-new model with no migration file -- it doesn't need one, `create_tables()`
builds it from `models.py` on first boot like any new table. That is exactly what makes it dangerous
to ship early: creating a new table is what flags an app for the initial-setup fake-apply described
above. **`RaceResult` must not reach a server before migration `001` has actually run there** (confirm
with `DESCRIBE bet;`, per the top of this file) -- ship it in a second, later deploy, never the same
one as a still-pending migration.
