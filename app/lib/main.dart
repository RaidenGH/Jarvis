import 'package:flutter/material.dart';

import 'chat_screen.dart';
import 'theme.dart';

void main() => runApp(const JarvisApp());

class JarvisApp extends StatelessWidget {
  const JarvisApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Jarvis',
      debugShowCheckedModeBanner: false,
      // The glass palette from `New UI.html` — pinned dark, it's the design.
      theme: buildHudTheme(),
      themeMode: ThemeMode.dark,
      darkTheme: buildHudTheme(),
      home: const ChatScreen(),
    );
  }
}
