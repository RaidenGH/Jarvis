import 'package:flutter_test/flutter_test.dart';
import 'package:jarvis_app/transcript.dart';

void main() {
  group('assistant bubbles', () {
    test('a reply opens its own bubble instead of joining the user message',
        () {
      // The bug this guards: keying off a "busy" flag made the reply append
      // to the user's own bubble, so "hi" and the answer rendered as one.
      final transcript = Transcript()
        ..addUser('hi')
        ..appendAssistantToken('Hello!');

      expect(transcript.entries, hasLength(2));
      expect((transcript.entries[0] as UserEntry).text, 'hi');
      expect((transcript.entries[1] as AssistantEntry).text, 'Hello!');
    });

    test('consecutive tokens accumulate into a single bubble', () {
      final transcript = Transcript()
        ..addUser('hi')
        ..appendAssistantToken('Hel')
        ..appendAssistantToken('lo')
        ..appendAssistantToken('!');

      expect(transcript.entries, hasLength(2));
      expect((transcript.entries[1] as AssistantEntry).text, 'Hello!');
    });

    test('a second turn opens a second bubble', () {
      final transcript = Transcript()
        ..addUser('one')
        ..appendAssistantToken('first')
        ..addUser('two')
        ..appendAssistantToken('second');

      expect(
        transcript.entries.whereType<AssistantEntry>().map((e) => e.text),
        ['first', 'second'],
      );
    });
  });

  group('tool activity', () {
    test('a call is shown in flight, then resolves to its result', () {
      final transcript = Transcript()
        ..startToolCall(name: 'system_stats')
        ..finishTool('system_stats', {'cpu': 8});

      final entry = transcript.entries.single as ToolEntry;
      expect(entry.status, ToolStatus.done);
      expect(entry.name, 'system_stats');
      expect(entry.detail, contains('"cpu": 8'));
    });

    test('a confirm_request marks the call as awaiting approval', () {
      final transcript = Transcript()
        ..startToolCall(name: 'open_app', arguments: {'name': 'notepad'})
        ..toolAwaitingApproval('open_app',
            risk: 'reversible-write', mode: 'tap');

      final entry = transcript.entries.single as ToolEntry;
      expect(entry.status, ToolStatus.awaiting);
      expect(entry.risk, 'reversible-write');
      expect(entry.mode, 'tap');
    });

    test('a declined call records the reason and hides nothing', () {
      final transcript = Transcript()
        ..startToolCall(name: 'set_volume')
        ..toolDenied('set_volume',
            reason: 'the user did not confirm set_volume');

      final entry = transcript.entries.single as ToolEntry;
      expect(entry.status, ToolStatus.denied);
      expect(entry.detail, contains('did not confirm'));
    });

    test('a disabled call is distinguished from a decline', () {
      final transcript = Transcript()
        ..startToolCall(name: 'send_email')
        ..toolDenied('send_email',
            status: ToolStatus.disabled, reason: 'send_email is disabled');

      final entry = transcript.entries.single as ToolEntry;
      expect(entry.status, ToolStatus.disabled);
    });

    test('two calls to the same tool resolve to their own entries', () {
      final transcript = Transcript()
        ..startToolCall(name: 'system_stats', id: 'a')
        ..finishTool('system_stats', {'which': 'first'})
        ..startToolCall(name: 'system_stats', id: 'b')
        ..finishTool('system_stats', {'which': 'second'});

      final entries = transcript.entries.cast<ToolEntry>();
      expect(entries[0].detail, contains('first'));
      expect(entries[1].detail, contains('second'));
      expect(entries[0].status, ToolStatus.done);
    });

    test('a result for an untracked call still surfaces', () {
      // e.g. a tool result arriving after the transcript was cleared.
      final transcript = Transcript()..finishTool('system_stats', {'ok': true});

      final entry = transcript.entries.single as ToolEntry;
      expect(entry.status, ToolStatus.done);
    });

    test('a denied call that was never announced still surfaces', () {
      final transcript = Transcript()..toolDenied('open_app', reason: 'no');

      expect(
        (transcript.entries.single as ToolEntry).status,
        ToolStatus.denied,
      );
    });
  });

  test('clear empties the transcript', () {
    final transcript = Transcript()
      ..addUser('hi')
      ..appendAssistantToken('there')
      ..startToolCall(name: 'system_stats');

    transcript.clear();

    expect(transcript.isEmpty, isTrue);
    expect(transcript.length, 0);
  });

  group('argumentSummary', () {
    test('summarises the arguments on one line', () {
      final entry = ToolEntry(
        name: 'read_file',
        arguments: {'path': 'app/lib/main.dart'},
      );
      expect(entry.argumentSummary, 'path: app/lib/main.dart');
    });

    test('says so when a tool takes no arguments', () {
      expect(ToolEntry(name: 'system_stats').argumentSummary, 'no arguments');
    });

    test('shortens very long values', () {
      final entry = ToolEntry(
        name: 'read_file',
        arguments: {'path': 'x' * 200},
      );
      expect(entry.argumentSummary.length, lessThan(60));
      expect(entry.argumentSummary, endsWith('…'));
    });
  });

  group('previewOf', () {
    test('pretty-prints a result', () {
      expect(previewOf({'a': 1}), contains('"a": 1'));
    });

    test('caps a huge result so it cannot blow up the tree', () {
      final preview = previewOf({'blob': 'x' * 10000});
      expect(preview, contains('truncated'));
      expect(preview.length, lessThan(4200));
    });

    test('survives a value that cannot be encoded', () {
      expect(previewOf(double.nan), isNotEmpty);
    });
  });
}
