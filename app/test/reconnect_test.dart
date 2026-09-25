import 'dart:math' as math;

import 'package:flutter_test/flutter_test.dart';
import 'package:jarvis_app/reconnect.dart';

void main() {
  group('Backoff', () {
    test('grows geometrically, then holds at the ceiling', () {
      final backoff = Backoff();

      expect(backoff.next(), const Duration(seconds: 2));
      expect(backoff.next(), const Duration(seconds: 4));
      expect(backoff.next(), const Duration(seconds: 8));
      expect(backoff.next(), const Duration(seconds: 16));
      expect(backoff.next(), const Duration(seconds: 32));
      // Never waits longer than `max`, however long the backend stays down.
      for (var i = 0; i < 10; i++) {
        expect(backoff.next(), const Duration(seconds: 32));
      }
    });

    test('reset sends the next outage back to the first delay', () {
      final backoff = Backoff()..next()..next()..next();
      expect(backoff.attempt, 3);

      backoff.reset();

      expect(backoff.attempt, 0);
      expect(backoff.next(), const Duration(seconds: 2));
    });

    test('jitter stays inside the configured band', () {
      final backoff = Backoff(jitter: 0.25, random: math.Random(7));

      for (var i = 0; i < 40; i++) {
        final delay = backoff.next();
        expect(delay, greaterThan(Duration.zero));
        expect(delay, lessThanOrEqualTo(const Duration(seconds: 32)));
      }
    });

    test('a custom schedule is honoured', () {
      final backoff = Backoff(
        first: const Duration(milliseconds: 250),
        factor: 3,
        max: const Duration(seconds: 5),
      );

      expect(backoff.next(), const Duration(milliseconds: 250));
      expect(backoff.next(), const Duration(milliseconds: 750));
      expect(backoff.next(), const Duration(milliseconds: 2250));
      expect(backoff.next(), const Duration(seconds: 5));
    });
  });
}
