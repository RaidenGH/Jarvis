import 'package:flutter_test/flutter_test.dart';
import 'package:jarvis_app/palette.dart';

void main() {
  const actions = [
    PaletteAction(
      id: 'reset',
      label: 'New session',
      keywords: ['clear', 'restart'],
    ),
    PaletteAction(id: 'reconnect', label: 'Reconnect to backend'),
    PaletteAction(
      id: 'settings',
      label: 'Settings & backend info',
      keywords: ['config', 'tools'],
    ),
    PaletteAction(id: 'wake', label: 'Always-listen for wake word'),
  ];

  group('rankActions', () {
    test('an empty query returns everything in the authored order', () {
      expect(
        rankActions(actions, '   ').map((a) => a.id),
        actions.map((a) => a.id),
      );
    });

    test('a substring hit wins', () {
      expect(rankActions(actions, 'reconn').first.id, 'reconnect');
    });

    test('a prefix hit outranks a mid-word one', () {
      // "settings" starts with the query; "new session" only contains it.
      expect(rankActions(actions, 'se').first.id, 'settings');
    });

    test('initials match fuzzily', () {
      expect(rankActions(actions, 'ns').first.id, 'reset');
    });

    test('keywords are searchable even though they are not shown', () {
      expect(rankActions(actions, 'clear').first.id, 'reset');
      expect(rankActions(actions, 'tools').first.id, 'settings');
    });

    test('nothing matches meaningless input', () {
      expect(rankActions(actions, 'qqqqq'), isEmpty);
    });

    test('an ask row sorts after real matches', () {
      final results = rankActions(actions, 'wake');
      expect(results.first.id, 'wake');
    });
  });
}
