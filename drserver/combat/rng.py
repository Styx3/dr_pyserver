"""Mersenne Twister MT19937 — deterministic pseudorandom number generator.

Ported from C# Combat/MersenneTwister.cs. Binary-exact MT19937 implementation
used for shared RNG seed synchronization between server and client. The client
consumes RNG values during combat to ensure deterministic damage/hit outcomes.

The server shares the seed via opcode 0x0C (RNG seed sync), and all parties
consume values from the same generator at the same rate.
"""
from __future__ import annotations


class MersenneTwister:
    """MT19937 — 32-bit output, binary-exact with C# Combat/MersenneTwister.cs.

    The DR client's non-standard tempering masks reduce to the canonical MT19937
    masks ((y & A) << s == (y << s) & (A << s)), so this is plain MT19937:
        0xFF3A58AD << 7  == 0x9D2C5680   (tempering mask b)
        0xFFFFDF8C << 15 == 0xEFC60000   (tempering mask c)

    API mirrors the C# surface used by the native damage-replay engine:
    ``generate()``, ``generate_range(min, max)``, ``generate_int(min, max)``, and
    the ``last_seed`` / ``calls_since_reseed`` / ``last_generated_value``
    diagnostics. ``genrand_int32`` is kept as an alias of ``generate``.
    """

    N = 624
    M = 397
    MATRIX_A = 0x9908B0DF
    UPPER_MASK = 0x80000000
    LOWER_MASK = 0x7FFFFFFF
    # C# Generate() lazily seeds this when Seed() was never called.
    _DEFAULT_SEED = 0x1105

    def __init__(self, seed: int | None = None):
        self._mt = [0] * self.N
        self._mti = self.N + 1  # N+1 means uninitialized (matches C#).
        self.last_seed = 0
        self.calls_since_reseed = 0
        self.last_generated_value = 0
        if seed is not None:
            self.seed(seed)

    def seed(self, s: int) -> None:
        """Seed the generator (C# Seed())."""
        s &= 0xFFFFFFFF
        self.last_seed = s
        self.calls_since_reseed = 0
        self._mt[0] = s
        for i in range(1, self.N):
            self._mt[i] = (1812433253 * (self._mt[i - 1] ^ (self._mt[i - 1] >> 30)) + i) & 0xFFFFFFFF
        self._mti = self.N  # Force a twist on the next generate.

    # Back-compat alias for callers using the canonical mt19937ar name.
    def init_genrand(self, s: int) -> None:
        self.seed(s)

    def generate(self) -> int:
        """Generate a tempered 32-bit value (C# Generate())."""
        if self._mti >= self.N:
            if self._mti == self.N + 1:
                self.seed(self._DEFAULT_SEED)
            self._twist()

        y = self._mt[self._mti]
        self._mti += 1

        y ^= (y >> 11)
        y ^= ((y << 7) & 0x9D2C5680)
        y ^= ((y << 15) & 0xEFC60000)
        y ^= (y >> 18)
        y &= 0xFFFFFFFF

        self.calls_since_reseed += 1
        self.last_generated_value = y
        return y

    # Back-compat alias used by WanderSimulator.
    def genrand_int32(self) -> int:
        return self.generate()

    def generate_range(self, min_value: int, max_value: int) -> int:
        """Inclusive [min, max] unsigned draw (C# Generate(uint, uint))."""
        rng = (max_value - min_value + 1) & 0xFFFFFFFF
        if rng == 0:
            return min_value
        return (self.generate() % rng) + min_value

    def generate_int(self, min_value: int, max_value: int) -> int:
        """Inclusive [min, max] signed draw (C# GenerateInt(int, int))."""
        if max_value < min_value:
            return min_value
        rng = (max_value - min_value + 1) & 0xFFFFFFFF
        return ((self.generate() % rng) + min_value) if rng else min_value

    def _twist(self) -> None:
        for kk in range(self.N):
            y = (self._mt[kk] & self.UPPER_MASK) | (self._mt[(kk + 1) % self.N] & self.LOWER_MASK)
            self._mt[kk] = self._mt[(kk + self.M) % self.N] ^ (y >> 1)
            if y & 1:
                self._mt[kk] ^= self.MATRIX_A
        self._mti = 0


class WanderSimulator:
    """Deterministic monster wandering RNG simulator.

    Ported from C# Combat/WanderSimulator.cs. Binary-exact state machine that
    pre-computes RNG consumption so the server's RNG stays in sync with the
    client's RNG during monster idle/wander behavior.

    State machine (binary-verified from DR.exe PDB):
      State 0 → State 3 (no RNG)
      State 1 → 2 RNG (X,Y offset) → State 2
      State 2 → arrival check → State 3
      State 3 → 1 RNG (timer = result%150 + 90, *3 if canWander) → State 4
      State 4 → timer countdown, then 1 RNG (%100, <30 → State 1, else timer=450)
    """

    def __init__(self, rng: MersenneTwister, can_wander: bool = True):
        self._rng = rng
        self._can_wander = can_wander
        self._state = 3       # Start in pause state.
        self._timer = 0
        self._pos_x = 0.0
        self._pos_y = 0.0

    def tick(self) -> int:
        """Advance the wander simulation by one tick. Returns RNG consumed count."""
        rng_used = 0

        if self._state == 1:          # Moving — consume 2 RNG for position offset.
            self._rng.genrand_int32()   # X offset
            self._rng.genrand_int32()   # Y offset
            rng_used += 2
            self._state = 2

        elif self._state == 2:        # Arriving — check if arrived.
            self._state = 3

        elif self._state == 3:        # Paused — consume 1 RNG for next timer.
            val = self._rng.genrand_int32()
            rng_used += 1
            timer = (val % 150) + 90
            if self._can_wander:
                timer *= 3
            self._timer = timer
            self._state = 4

        elif self._state == 4:        # Timer countdown.
            if self._timer > 0:
                self._timer -= 1
            if self._timer <= 0:
                val = self._rng.genrand_int32()
                rng_used += 1
                if (val % 100) < 30:        # 30% chance to move.
                    self._state = 1
                else:
                    self._timer = 450       # Stay put for ~450 ticks.
                    self._state = 4

        return rng_used

    def sync_rng(self, ticks: int = 100) -> int:
        """Pre-advance the RNG for the given number of ticks. Returns total RNG consumed."""
        total = 0
        for _ in range(ticks):
            total += self.tick()
        return total
