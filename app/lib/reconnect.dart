import 'dart:math' as math;

/// Exponential backoff for reconnection attempts.
///
/// The shell points at a backend you start by hand, so "backend is down" is
/// the normal first state, not an error — retrying forever on a growing delay
/// means you can start uvicorn ten minutes later and the UI just picks it up.
///
/// Kept free of Flutter imports so the schedule is unit-testable.
class Backoff {
  Backoff({
    this.first = const Duration(seconds: 2),
    this.factor = 2,
    this.max = const Duration(seconds: 32),
    this.jitter = 0,
    math.Random? random,
  }) : _random = random ?? math.Random();

  /// Delay before the first retry.
  final Duration first;

  /// Multiplier applied per attempt.
  final double factor;

  /// Ceiling for the delay.
  final Duration max;

  /// Fractional randomness applied to each delay (0 = perfectly regular).
  final double jitter;

  final math.Random _random;
  int _attempt = 0;

  /// How many delays have been handed out since the last [reset].
  int get attempt => _attempt;

  /// The next delay, advancing the schedule.
  Duration next() {
    final raw = first.inMilliseconds * math.pow(factor, _attempt);
    final capped = math.min(raw.toDouble(), max.inMilliseconds.toDouble());
    _attempt++;
    if (jitter <= 0) return Duration(milliseconds: capped.round());
    final spread = capped * jitter;
    final ms = (capped - spread + _random.nextDouble() * spread * 2)
        .round()
        .clamp(0, max.inMilliseconds);
    return Duration(milliseconds: ms);
  }

  /// Call on a successful connection: the next outage starts from [first].
  void reset() => _attempt = 0;
}
