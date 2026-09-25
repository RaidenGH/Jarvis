import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:jarvis_app/main.dart';

void main() {
  // The shell retries a dead backend on a timer, so each test unmounts the
  // tree to cancel it before the test ends.
  Future<void> unmount(WidgetTester tester) async {
    await tester.pumpWidget(const SizedBox());
    await tester.pump();
  }

  testWidgets('renders the glass shell and reports the backend as offline',
      (tester) async {
    await tester.pumpWidget(const JarvisApp());
    await tester.pump();

    expect(find.text('Jarvis'), findsOneWidget);
    expect(find.text('Transcript'), findsOneWidget);
    expect(find.text('HOLD'), findsOneWidget);
    expect(find.text('WAKE'), findsOneWidget);
    // Backend isn't running in tests, so both caption and hint say so.
    expect(find.textContaining('Backend offline'), findsWidgets);

    await unmount(tester);
  });

  // The mock is a phone-sized column; the shell runs in a resizable desktop
  // window, so none of these may overflow (a RenderFlex overflow fails the
  // test outright).
  for (final size in const [Size(640, 480), Size(900, 700), Size(1440, 1000)]) {
    testWidgets('lays out at ${size.width.toInt()}x${size.height.toInt()}',
        (tester) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);

      await tester.pumpWidget(const JarvisApp());
      await tester.pump();

      expect(tester.takeException(), isNull);
      await unmount(tester);
    });
  }

  testWidgets('opens the command palette on Ctrl+K', (tester) async {
    await tester.pumpWidget(const JarvisApp());
    await tester.pump();

    await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
    await tester.sendKeyDownEvent(LogicalKeyboardKey.keyK);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.keyK);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
    await tester.pump(const Duration(milliseconds: 250));

    expect(find.text('New session'), findsOneWidget);
    expect(find.text('Reconnect to backend'), findsOneWidget);

    // Escape closes it again.
    await tester.sendKeyEvent(LogicalKeyboardKey.escape);
    await tester.pump(const Duration(milliseconds: 250));
    expect(find.text('New session'), findsNothing);

    await unmount(tester);
  });

  testWidgets('typing anywhere opens the composer with that keystroke',
      (tester) async {
    await tester.pumpWidget(const JarvisApp());
    await tester.pump();

    await tester.sendKeyDownEvent(LogicalKeyboardKey.keyH, character: 'h');
    await tester.pump(const Duration(milliseconds: 250));

    // Matches survive on top; the composer adds its own row underneath.
    expect(find.text('Ask Jarvis'), findsOneWidget);
    expect(find.text('Reconnect to backend'), findsOneWidget);
    expect(tester.takeException(), isNull);

    await unmount(tester);
  });

  testWidgets('Ctrl+, opens settings on a small window without overflowing',
      (tester) async {
    tester.view.physicalSize = const Size(640, 480);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(const JarvisApp());
    await tester.pump();

    await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
    await tester.sendKeyDownEvent(LogicalKeyboardKey.comma);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.comma);
    await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
    await tester.pump(const Duration(milliseconds: 250));

    expect(find.text('Settings'), findsOneWidget);
    expect(find.text('connection'.toUpperCase()), findsOneWidget);
    expect(tester.takeException(), isNull);

    await unmount(tester);
  });
}
