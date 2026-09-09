import 'package:flutter_test/flutter_test.dart';

import 'package:jarvis_app/main.dart';

void main() {
  testWidgets('Jarvis shell renders and shows the chat placeholder',
      (tester) async {
    await tester.pumpWidget(const JarvisApp());
    await tester.pump();

    expect(find.text('Jarvis'), findsOneWidget);
    // Backend isn't running in tests, so the shell shows the offline hint.
    expect(find.textContaining('Backend offline'), findsOneWidget);
  });
}