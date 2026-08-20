"""
Rebuild past races out of the statistics PyPlanet was already keeping, so the odds of
docs/conception-paris-position.md do not have to wait thirty races to mean anything.

`stats_scores` holds every finish ever driven on the server, each one stamped with the moment it
happened. That is more than a table of personal bests: finishes on the same map that fall close
together in time were driven by people who were on the server at the same time -- a race. Cutting
that stream apart at the quiet gaps recovers those races, one row per driver, in exactly the shape
the podium recorder writes.

Two things make this worth more here than it would be in most games. TrackMania 2 stopped being
updated more than a decade ago, so its physics and its maps are the same today as they were then:
a time from 2011 measures exactly what a time from 2026 measures, and neither deserves to be
discounted for its age. And a server like lolmaps has fifteen years of this lying in its database
already -- tens of thousands of races that would otherwise have to be waited for one map at a time.

What the reconstruction cannot recover is stated plainly rather than papered over:

- Only drivers who finished appear, because only they left a score behind. That is the same rule
  the live recorder follows, so the two kinds of row mean the same thing.
- There are no round points, only times, so imported races look like time attack whatever mode was
  actually being played.
- The session gap is a judgement call, not a fact. Guess too generously and two evenings merge into
  one race that never took place -- which is why every imported row is marked as imported and can
  be thrown away without touching a race that was really watched.
"""
import datetime
import logging

from pyplanet.apps.core.statistics.models import Score

from .models import RaceResult

logger = logging.getLogger(__name__)

DEFAULT_GAP_MINUTES = 15
"""
Quiet time on one map that separates two races.

A map is rarely played for more than a quarter of an hour at a stretch, and a server that moves on
does not come back to the same map minutes later. Fifteen minutes is comfortably longer than a map
and comfortably shorter than the wait before it comes round again.
"""

MAX_FIELD_SIZE = 250
"""
Above this many finishers, the cluster is not a race.

A map that stayed in rotation through a whole event can leave a run of finishes with no gap big
enough to cut, and the result would be a single "race" with hundreds of drivers who never saw each
other. Cheaper to drop those than to explain later why one map produced an implausible field.
"""


class RaceImporter:
	"""
	Turns `stats_scores` into `RaceResult` rows, and reports what it did in enough detail to be
	trusted or overruled.

	Runs in two passes on purpose: `preview` reads and writes nothing, `run` does the same work and
	then commits it. An import that rewrites years of history should be something an admin has
	already seen the shape of before they agree to it.
	"""

	def __init__(self, app):
		self.app = app

	async def cutoff(self):
		"""
		The moment the plugin started watching races for itself.

		Everything from here on is recorded live, and importing across this line would file a second,
		reconstructed copy of a race that was already written down properly. Returns None when nothing
		has been recorded live yet, in which case the whole of the statistics is fair game.
		"""
		rows = list(await RaceResult.execute(
			RaceResult.select(RaceResult.created_at)
			.where(RaceResult.source == RaceResult.SOURCE_LIVE)
			.order_by(RaceResult.created_at.asc()).limit(1)
		))
		return rows[0].created_at if rows else None

	async def _map_ids(self):
		rows = list(await Score.execute(Score.select(Score.map_id).distinct()))
		return sorted({row.map_id for row in rows})

	async def _sessions_for_map(self, map_id, gap, cutoff):
		"""
		Cut one map's finishes into races, best first within each.

		Fetches a single map at a time rather than the whole table: on a server with fifteen years of
		statistics the full set does not belong in memory, and one map's worth always does.
		"""
		query = Score.select(Score.player_id, Score.score, Score.created_at).where(Score.map_id == map_id)
		if cutoff is not None:
			query = query.where(Score.created_at < cutoff)
		finishes = list(await Score.execute(query.order_by(Score.created_at.asc())))

		sessions = []
		current = []
		previous_at = None
		for finish in finishes:
			if previous_at is not None and (finish.created_at - previous_at) > gap:
				sessions.append(current)
				current = []
			current.append(finish)
			previous_at = finish.created_at
		if current:
			sessions.append(current)

		races = []
		for session in sessions:
			# One driver, one result: the best they managed while the map was up, which is what the live
			# recorder reads off the podium too. Several finishes by the same person is a driver retrying,
			# not several competitors.
			best = {}
			for finish in session:
				known = best.get(finish.player_id)
				if known is None or finish.score < known.score:
					best[finish.player_id] = finish

			if len(best) < 2 or len(best) > MAX_FIELD_SIZE:
				# Same rule as the live recorder: alone is not a race. See _record_race_results.
				continue

			ordered = sorted(best.values(), key=lambda f: f.score)
			races.append((session[0].created_at, ordered))
		return races

	async def collect(self, gap_minutes=DEFAULT_GAP_MINUTES):
		"""
		Reconstruct every importable race, as rows ready for RaceResult.

		Returns (rows, stats). Reads only -- `preview` and `run` share this so what an admin is shown
		is what an admin gets.
		"""
		gap = datetime.timedelta(minutes=gap_minutes)
		cutoff = await self.cutoff()

		rows = []
		races = 0
		maps = 0
		earliest = None
		latest = None

		for map_id in await self._map_ids():
			found = await self._sessions_for_map(map_id, gap, cutoff)
			if not found:
				continue
			maps += 1
			for happened_at, ordered in found:
				races += 1
				earliest = happened_at if earliest is None else min(earliest, happened_at)
				latest = happened_at if latest is None else max(latest, happened_at)
				for position, finish in enumerate(ordered, start=1):
					rows.append(dict(
						map=map_id,
						player=finish.player_id,
						source=RaceResult.SOURCE_IMPORT,
						position=position,
						field_size=len(ordered),
						points=None,
						time=finish.score,
						# Stamped with when the race happened, not when the import ran: these rows are
						# evidence about a date, and dating them today would lose that.
						created_at=happened_at,
						updated_at=happened_at,
					))

		return rows, dict(
			races=races, rows=len(rows), maps=maps,
			earliest=earliest, latest=latest, cutoff=cutoff,
			drivers=len({row['player'] for row in rows}),
		)

	async def existing(self):
		rows = list(await RaceResult.execute(
			RaceResult.select().where(RaceResult.source == RaceResult.SOURCE_IMPORT)
		))
		return len(rows)

	async def clear(self):
		"""Remove every imported row. Live rows are untouched -- that is what the source column is for."""
		return await RaceResult.execute(
			RaceResult.delete().where(RaceResult.source == RaceResult.SOURCE_IMPORT)
		)

	async def run(self, gap_minutes=DEFAULT_GAP_MINUTES):
		"""
		Replace the imported history with a fresh reconstruction.

		Clears first so running it twice does not double the history, and so a gap that turned out to be
		badly chosen can simply be run again with a better one.
		"""
		rows, stats = await self.collect(gap_minutes=gap_minutes)
		removed = await self.clear()

		# In batches because a fifteen-year import is far past what one INSERT should carry.
		for start in range(0, len(rows), 500):
			await RaceResult.execute(RaceResult.insert_many(rows[start:start + 500]))

		stats['removed'] = removed
		logger.info(
			'BetPluggin: imported %d race(s) as %d row(s) from the server statistics, replacing %s.',
			stats['races'], stats['rows'], removed
		)
		return stats
