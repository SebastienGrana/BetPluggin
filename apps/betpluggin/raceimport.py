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

Which is also why nothing here is allowed to hold the whole job in memory. Fifteen years of
finishes is not a list you build and then write; it is a stream you walk once, keeping only the
race currently being assembled. See `_iter_races`.

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

PAGE_SIZE = 5000
"""
Finishes read per round trip.

Small enough that one page is nothing to hold, large enough that a map with a hundred thousand
finishes is twenty queries rather than a hundred thousand.
"""

INSERT_BATCH = 500
"""Rows per INSERT. A fifteen-year import is far past what one statement should carry."""


class RaceImporter:
	"""
	Turns `stats_scores` into `RaceResult` rows, and reports what it did in enough detail to be
	trusted or overruled.

	Reads in two modes on purpose: `preview` counts and writes nothing, `run` does the same walk and
	commits it. An import that rewrites years of history should be something an admin has already
	seen the shape of before they agree to it.
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

	@staticmethod
	def _close_session(best, started_at, oversized):
		"""One assembled session, or None when it was never a race worth keeping."""
		if oversized or len(best) < 2:
			# Same rule as the live recorder: alone is not a race. See _record_race_results.
			return None
		return started_at, sorted(best.values(), key=lambda f: f.score)

	async def _iter_races(self, map_id, gap, cutoff):
		"""
		Walk one map's finishes once, yielding (happened_at, ordered_finishes) per race.

		Reads in pages keyed on the primary key rather than fetching the map in one go, and keeps only
		the session being assembled -- so the memory this costs is set by how many people were on the
		server at once, not by how many finishes the map has collected since 2011.

		Paging on `id` rather than on `created_at` is what makes those pages cheap: `stats_scores` is
		indexed on map_id alone, so ordering by time would mean sorting the whole map on every page,
		while ordering by id is served straight from that index. Finishes are appended as they happen,
		so the two orders agree; where they somehow do not, the effect is a pair of finishes not
		splitting a session that a stricter reading would have split.
		"""
		last_id = 0
		best = {}
		started_at = None
		previous_at = None
		oversized = False

		while True:
			query = Score.select(Score.id, Score.player_id, Score.score, Score.created_at).where(
				(Score.map_id == map_id) & (Score.id > last_id)
			)
			if cutoff is not None:
				query = query.where(Score.created_at < cutoff)
			page = list(await Score.execute(query.order_by(Score.id.asc()).limit(PAGE_SIZE)))
			if not page:
				break

			for finish in page:
				last_id = finish.id

				if previous_at is not None and (finish.created_at - previous_at) > gap:
					race = self._close_session(best, started_at, oversized)
					if race:
						yield race
					best, started_at, oversized = {}, None, False

				previous_at = finish.created_at
				if started_at is None:
					started_at = finish.created_at

				if oversized:
					continue

				# One driver, one result: the best they managed while the map was up, which is what the
				# live recorder reads off the podium too. Several finishes by the same person is a driver
				# retrying, not several competitors.
				known = best.get(finish.player_id)
				if known is None or finish.score < known.score:
					best[finish.player_id] = finish

				if len(best) > MAX_FIELD_SIZE:
					# Dropped, and dropped now rather than at the end: an implausible field is exactly the
					# case where holding on to it costs the most.
					oversized = True
					best = {}

			if len(page) < PAGE_SIZE:
				break

		race = self._close_session(best, started_at, oversized)
		if race:
			yield race

	@staticmethod
	def _rows_for(map_id, happened_at, ordered):
		return [dict(
			map=map_id,
			player=finish.player_id,
			source=RaceResult.SOURCE_IMPORT,
			position=position,
			field_size=len(ordered),
			points=None,
			time=finish.score,
			# Stamped with when the race happened, not when the import ran: these rows are evidence
			# about a date, and dating them today would lose that.
			created_at=happened_at,
			updated_at=happened_at,
		) for position, finish in enumerate(ordered, start=1)]

	@staticmethod
	def _blank_stats(cutoff):
		return dict(races=0, rows=0, maps=0, earliest=None, latest=None, cutoff=cutoff, drivers=0)

	@staticmethod
	def _account(stats, drivers, map_id, happened_at, ordered):
		stats['races'] += 1
		stats['rows'] += len(ordered)
		stats['earliest'] = happened_at if stats['earliest'] is None else min(stats['earliest'], happened_at)
		stats['latest'] = happened_at if stats['latest'] is None else max(stats['latest'], happened_at)
		drivers.update(finish.player_id for finish in ordered)

	async def preview(self, gap_minutes=DEFAULT_GAP_MINUTES):
		"""
		Count what an import would produce, writing nothing.

		Walks exactly what `run` walks, so the numbers an admin agrees to are the numbers they get.
		Keeps no rows: only the tally, and the set of drivers seen, which is bounded by how many people
		have ever played on the server rather than by how much they played.
		"""
		gap = datetime.timedelta(minutes=gap_minutes)
		cutoff = await self.cutoff()
		stats = self._blank_stats(cutoff)
		drivers = set()

		for map_id in await self._map_ids():
			counted = stats['races']
			async for happened_at, ordered in self._iter_races(map_id, gap, cutoff):
				self._account(stats, drivers, map_id, happened_at, ordered)
			if stats['races'] > counted:
				stats['maps'] += 1

		stats['drivers'] = len(drivers)
		return stats

	async def existing(self):
		"""How many imported rows are stored. Counted in the database, not by fetching them."""
		return await RaceResult.objects.count(
			RaceResult.select().where(RaceResult.source == RaceResult.SOURCE_IMPORT)
		)

	async def clear(self):
		"""Remove every imported row. Live rows are untouched -- that is what the source column is for."""
		return await RaceResult.execute(
			RaceResult.delete().where(RaceResult.source == RaceResult.SOURCE_IMPORT)
		)

	async def run(self, gap_minutes=DEFAULT_GAP_MINUTES):
		"""
		Replace the imported history with a fresh reconstruction.

		Clears before writing rather than after, because the rows are streamed out as they are found
		and there is never a complete new history to swap in. The cost of that order is that an import
		interrupted halfway leaves a partial one behind; the cost of the other order would be holding
		fifteen years of rows in memory to avoid it. Running it again fixes a partial import, which is
		the same thing that fixes a badly chosen gap.
		"""
		gap = datetime.timedelta(minutes=gap_minutes)
		cutoff = await self.cutoff()
		stats = self._blank_stats(cutoff)
		drivers = set()

		stats['removed'] = await self.clear()
		pending = []

		for map_id in await self._map_ids():
			counted = stats['races']
			async for happened_at, ordered in self._iter_races(map_id, gap, cutoff):
				self._account(stats, drivers, map_id, happened_at, ordered)
				pending.extend(self._rows_for(map_id, happened_at, ordered))
				while len(pending) >= INSERT_BATCH:
					await RaceResult.execute(RaceResult.insert_many(pending[:INSERT_BATCH]))
					del pending[:INSERT_BATCH]
			if stats['races'] > counted:
				stats['maps'] += 1

		if pending:
			await RaceResult.execute(RaceResult.insert_many(pending))

		stats['drivers'] = len(drivers)
		logger.info(
			'BetPluggin: imported %d race(s) as %d row(s) from the server statistics, replacing %s.',
			stats['races'], stats['rows'], stats['removed']
		)
		return stats
