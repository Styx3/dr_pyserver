"""Infer the client's room-RNG position from observed events (ROUTE 2B, Step 5).

Port of DR-Server's ``Combat/RngPositionInferrer.cs`` (762 lines).  The client's
MT19937 stream is deterministic from the server-sent seed (opcode 0x0C).  Each
observed wander packet (0x65) tells us ``MT[N] % span == rawXmodded`` and
``MT[N+1] % span == rawYmodded`` for an unknown client RNG position ``N``; the
inferrer caches the MT outputs and scans for matching positions, intersecting
multiple observations to pin the drift between the client's and server's RNG
positions.  ``RefineDriftFromDamage`` then brute-forces the exact drift by
replaying ``compute_swing`` until the observed damage reproduces.

This is the layer that aligns the server's RNG stream to the captured client
swing stream (ch7/0x34 ``0904 0100 50 <sid> <target>``), so Step 6's kill
detection resolves the same damage the client did.

Pinned against the embedded ``RngPositionInferrerSelfTest.RunAll`` oracle
(tests/test_rng_position_inferrer.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from drserver.combat.monster_damage import MonsterUnitStats, PlayerUnitStats, compute_swing
from drserver.combat.rng import MersenneTwister

_U32 = 0xFFFFFFFF


@dataclass
class WanderObservation:
    """One observed wander roll (RngPositionInferrer.cs:264)."""

    raw_x_modded: int = 0           # (MT[N] % span) recovered from observed posX
    raw_y_modded: int = 0           # (MT[N+1] % span) recovered from observed posY
    span: int = 0                   # 2 * WanderRange — the modulus the client used
    server_position_at_event: int = 0  # server _roomRng.CallsSinceReseed at the roll


def compute_raw_modded(target_coord: float, anchor_coord: float, range_: int) -> int:
    """``WanderObservation.ComputeRawModded`` (cs:283).

    The client did ``targetX = anchorX + (int)(rawX % span) - range``, so
    ``(uint)(targetX - anchorX + range) == rawX % span``.  C# ``Math.Round`` and
    Python ``round`` are both round-half-to-even.
    """
    return (round(target_coord - anchor_coord) + range_) & _U32


class RngPositionInferrer:
    """Ports ``RngPositionInferrer`` (cs:18)."""

    def __init__(self, seed: int, cache_size: int = 10000):
        self.seed = seed & _U32
        self.cache_size = cache_size
        # _outputs[i] = MT output for position (i+1); position 1 = first generate().
        mt = MersenneTwister(self.seed)
        self._outputs = [mt.generate() for _ in range(cache_size)]

    def output_at_position(self, position: int) -> int:
        """``OutputAtPosition`` (cs:47) — 1-indexed."""
        if position < 1 or position > self.cache_size:
            raise ValueError(
                f"position={position} outside cache [1..{self.cache_size}]"
            )
        return self._outputs[position - 1]

    def find_matching_wander_positions(
        self, raw_x_modded: int, raw_y_modded: int, span: int,
        search_start: int, search_end: int,
    ) -> list[int]:
        """``FindMatchingWanderPositions`` (cs:61)."""
        if span == 0:
            raise ValueError("span must be > 0")
        if search_start < 1:
            search_start = 1
        if search_end > self.cache_size - 1:  # need N+1
            search_end = self.cache_size - 1

        outputs = self._outputs
        matches: list[int] = []
        for n in range(search_start, search_end + 1):
            if outputs[n - 1] % span == raw_x_modded and outputs[n] % span == raw_y_modded:
                matches.append(n)
        return matches

    def infer_drift(
        self, observations: list[WanderObservation], drift_window: int = 5000
    ) -> int:
        """``InferDrift`` (cs:88) — intersect candidate drifts; -1 if not unique."""
        if not observations:
            return -1

        candidate_drifts: set[int] | None = None
        for obs in observations:
            matches = self.find_matching_wander_positions(
                obs.raw_x_modded, obs.raw_y_modded, obs.span,
                obs.server_position_at_event,
                obs.server_position_at_event + drift_window,
            )
            this_drifts = {m - obs.server_position_at_event for m in matches}

            if candidate_drifts is None:
                candidate_drifts = this_drifts
            else:
                candidate_drifts &= this_drifts

            if len(candidate_drifts) == 0:
                return -1
            if len(candidate_drifts) == 1:
                return next(iter(candidate_drifts))

        if candidate_drifts is not None and len(candidate_drifts) == 1:
            return next(iter(candidate_drifts))
        return -1

    def infer_drift_trajectory(
        self, observations: list[WanderObservation], drift_window: int = 10000
    ) -> list[int] | None:
        """``InferDriftTrajectory`` (cs:134) — smoothest monotonic path."""
        if not observations:
            return None

        per_obs_drifts: list[list[int]] = []
        for obs in observations:
            matches = self.find_matching_wander_positions(
                obs.raw_x_modded, obs.raw_y_modded, obs.span,
                obs.server_position_at_event,
                obs.server_position_at_event + drift_window,
            )
            drifts = sorted(m - obs.server_position_at_event for m in matches)
            per_obs_drifts.append(drifts)

        paths: list[list[int]] = []
        max_paths = 50000
        self._enumerate_monotonic_paths(per_obs_drifts, 0, 0, [], paths, max_paths)
        if not paths:
            return None
        if len(paths) == 1:
            return paths[0]

        best_score = float("inf")
        best_path: list[int] | None = None
        for path in paths:
            score = self._smoothness_score(path, observations)
            if score < best_score:
                best_score = score
                best_path = path
        return best_path

    @staticmethod
    def _enumerate_monotonic_paths(
        per_obs_drifts: list[list[int]], event_idx: int, floor: int,
        current_path: list[int], output: list[list[int]], max_paths: int,
    ) -> None:
        """``EnumerateMonotonicPaths`` (cs:172)."""
        if len(output) >= max_paths:
            return
        if event_idx == len(per_obs_drifts):
            output.append(list(current_path))
            return
        for d in per_obs_drifts[event_idx]:
            if d < floor:
                continue
            current_path.append(d)
            RngPositionInferrer._enumerate_monotonic_paths(
                per_obs_drifts, event_idx + 1, d, current_path, output, max_paths
            )
            current_path.pop()
            if len(output) >= max_paths:
                return

    @staticmethod
    def _smoothness_score(path: list[int], observations: list[WanderObservation]) -> float:
        """``SmoothnessScore`` (cs:243) — variance of per-step drift growth rate."""
        if len(path) < 2:
            return 0.0
        rates: list[float] = []
        for i in range(1, len(path)):
            srv_delta = (
                observations[i].server_position_at_event
                - observations[i - 1].server_position_at_event
            )
            if srv_delta <= 0:
                srv_delta = 1
            rates.append((path[i] - path[i - 1]) / srv_delta)
        mean = sum(rates) / len(rates)
        return sum((r - mean) ** 2 for r in rates)

    @staticmethod
    def refine_drift_from_damage(
        seed: int,
        server_position: int,
        drift_estimate: int,
        search_radius: int,
        observed_damage: int,
        attacker: MonsterUnitStats,
        target: PlayerUnitStats,
    ) -> int:
        """``RefineDriftFromDamage`` (cs:205).

        Brute-force the ±search_radius window around the drift estimate, replaying
        ``compute_swing`` from a fresh MT advanced ``server_position + d - 1`` times,
        and return the drift whose damage matches ``observed_damage``.  Returns -1
        for no match, -2 for ambiguous (multiple matches).
        """
        seed &= _U32
        found = -1
        match_count = 0
        for d in range(drift_estimate - search_radius, drift_estimate + search_radius + 1):
            if d < 0:
                continue
            advance = server_position + d - 1
            if advance < 0:
                continue
            mt = MersenneTwister(seed)
            for _ in range(advance):
                mt.generate()
            result = compute_swing(attacker, target, mt)
            if result.damage == observed_damage:
                if match_count == 0:
                    found = d
                match_count += 1
                if match_count > 1:
                    return -2  # ambiguous; need more signals
        return -1 if match_count == 0 else found
